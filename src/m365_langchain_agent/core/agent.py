"""RAG agent orchestrator — search, deduplicate, generate, cite.

Pure LangChain (no LangGraph). Takes a user query + optional conversation history,
searches the configured index, and returns a grounded answer with inline citations.
"""

import logging
import re
from collections import Counter
from typing import Optional, TypedDict
from urllib.parse import quote, urlparse, unquote

from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from m365_langchain_agent.config import settings, token_provider
from m365_langchain_agent.exceptions import GenerationError, RetrievalError
from m365_langchain_agent.core.prompts import (
    SYSTEM_PROMPT,
    STTM_SYSTEM_PROMPT,
    SUGGESTED_PROMPTS_PROMPT,
    QUERY_REWRITE_PROMPT,
    QUERY_REFINE_PROMPT,
    OUT_OF_SCOPE_ANSWER,
)
from m365_langchain_agent.core.search import get_search_client

logger = logging.getLogger(__name__)


class Source(TypedDict, total=False):
    index: int
    title: str
    url: str
    source_type: str
    file_name: str
    page_number: int
    chunk_index: int
    total_chunks: int
    score: float
    reranker_score: float
    preview: str


class AgentResult(TypedDict):
    answer: str
    sources: list[Source]
    raw_chunks: list[dict]
    full_prompt: str


# ---------------------------------------------------------------------------
# LLM builder
# ---------------------------------------------------------------------------

def _is_reasoning_model(deployment: str) -> bool:
    return deployment.startswith("o3") or deployment.startswith("o1")


def _build_llm(temperature: float | None = None, model_name: str | None = None) -> AzureChatOpenAI:
    deployment = model_name or settings.azure_openai_deployment_name
    api_version = settings.azure_openai_api_version
    is_reasoning = _is_reasoning_model(deployment)
    if is_reasoning:
        api_version = "2024-12-01-preview"
    kwargs = dict(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_ad_token_provider=token_provider,
        azure_deployment=deployment,
        api_version=api_version,
    )
    if not is_reasoning:
        kwargs["temperature"] = temperature if temperature is not None else settings.default_temperature
    return AzureChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Source formatting
# ---------------------------------------------------------------------------

def _extract_section_label(content: str) -> str | None:
    head = content[:500]
    sheet_match = re.search(
        r"(?:Sheet|Worksheet|Tab)\s*:\s*(.+?)(?:\n|$)", head, re.IGNORECASE
    )
    if sheet_match:
        name = sheet_match.group(1).strip()
        if name:
            return f"Sheet: {name}"

    heading_match = re.search(r"^#{1,2}\s+(.+)", content, re.MULTILINE)
    if heading_match:
        heading = heading_match.group(1).strip()
        if heading:
            return f"Section: {heading}"

    return None


def _build_sources(documents: list[dict]) -> list[Source]:
    base_titles = [
        d.get("document_title") or d.get("file_name") or "Untitled"
        for d in documents
    ]
    title_counts = Counter(base_titles)

    sources = []
    for i, d in enumerate(documents):
        content = d.get("content", "")
        preview = content[:200].strip()
        if len(content) > 200:
            preview += "..."

        raw_url = d.get("source_url", "")
        safe_url = quote(raw_url, safe="/:@?&#=") if raw_url else ""
        base_title = base_titles[i]

        title = base_title
        if title_counts[base_title] > 1:
            section_label = _extract_section_label(content)
            if section_label:
                title = f"{base_title} — {section_label}"

        sources.append(Source(
            index=i + 1,
            title=title,
            url=safe_url,
            source_type=d.get("source_type", ""),
            file_name=d.get("file_name", ""),
            page_number=d.get("page_number", 0),
            chunk_index=d.get("chunk_index", 0),
            total_chunks=d.get("total_chunks", 0),
            score=round(d.get("score", 0.0), 4),
            reranker_score=round(d.get("reranker_score", 0.0), 4) if d.get("reranker_score") else 0.0,
            preview=preview,
        ))
    return sources


def _filter_cited_sources(answer: str, sources: list[Source]) -> list[Source]:
    cited_indices = set(int(m) for m in re.findall(r"\[(\d{1,2})\]", answer))
    if not cited_indices:
        return sources
    filtered = [s for s in sources if s.get("index") in cited_indices]
    return filtered if filtered else sources


def _get_unique_source_names(documents: list[dict]) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for d in documents:
        name = d.get("document_title") or d.get("file_name") or "Untitled"
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _extract_logical_path(source_url: str) -> Optional[str]:
    """Extract the human-readable logical path from a blob storage URL.

    Strips the blob host and container, returning only the meaningful
    folder/file hierarchy.  Example:
        https://acct.blob.core.windows.net/container/NFCU-VA-WIKI/Release/PayGuard.md
        -> NFCU-VA-WIKI/Release/PayGuard.md

    Returns None if the URL is empty or has no path beyond the container.
    """
    if not source_url or not source_url.strip():
        return None
    try:
        parsed = urlparse(source_url)
        segments = parsed.path.strip("/").split("/", 1)
        if len(segments) < 2 or not segments[1]:
            return None
        return unquote(segments[1])
    except Exception:
        return None


def _format_context(documents: list[dict], all_document_names: list[str] | None = None) -> str:
    if not documents:
        return "No documents found."

    unique_sources = _get_unique_source_names(documents)
    hint = ""
    if len(unique_sources) > 1:
        display_names = all_document_names if all_document_names else unique_sources
        source_list = ", ".join(f'"{s}"' for s in display_names)
        extra = ""
        if all_document_names and len(all_document_names) > len(unique_sources):
            extra = (
                f" (Note: {len(all_document_names)} matching documents found in total; "
                f"detailed content was retrieved from {len(unique_sources)} of them.)"
            )
        hint = (
            f"NOTE: These documents come from multiple distinct sources: "
            f"{source_list}.{extra} Synthesize information from all relevant sources into "
            f"a single answer. Only ask for clarification if sources directly contradict each other.\n\n"
        )

    parts = []
    for i, d in enumerate(documents):
        title = d.get("document_title") or d.get("file_name") or "Untitled"
        header = f"[{i+1}] (Source: {title})"
        logical_path = _extract_logical_path(d.get("source_url", ""))
        if logical_path:
            header += f"\nPath: {logical_path}"
        parts.append(f"{header}\n{d['content']}")

    return hint + "\n\n".join(parts)


def format_sources_markdown(sources: list[Source]) -> str:
    """Format sources as markdown with clickable links — deduplicated by file name."""
    if not sources:
        return ""

    lines = []
    seen_names: set[str] = set()
    for s in sources:
        name = s.get("file_name") or s.get("title", "Untitled")
        if name in seen_names:
            continue
        seen_names.add(name)
        url = s.get("url", "")

        if url:
            safe_url = quote(url, safe="/:@?&#=")
            link = f"[{name}]({safe_url})"
        else:
            link = f"**{name}**"

        lines.append(f"[{s['index']}] {link}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query rewriting
# ---------------------------------------------------------------------------

async def _rewrite_query_with_history(
    query: str,
    conversation_history: list[dict],
    model_name: str | None = None,
) -> str:
    if not conversation_history:
        return query

    history_lines = []
    for turn in conversation_history[-4:]:
        role = "User" if turn["role"] == "user" else "Assistant"
        content = turn["content"][:300] if turn["role"] == "assistant" else turn["content"]
        history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines)
    messages = [
        SystemMessage(content=QUERY_REWRITE_PROMPT),
        HumanMessage(content=f"Conversation:\n{history_text}\n\nFollow-up question: {query}\n\nRewritten query:"),
    ]

    try:
        llm = _build_llm(temperature=0.0, model_name=model_name)
        response = await llm.ainvoke(messages)
        rewritten = response.content.strip().strip('"')
        logger.info("Query rewrite: '%s' → '%s'", query, rewritten)
        return rewritten
    except Exception as e:
        logger.warning("Query rewrite failed, using original: %s", e)
        return query


async def _refine_query_for_retry(query: str, model_name: str | None = None) -> str | None:
    messages = [
        SystemMessage(content=QUERY_REFINE_PROMPT),
        HumanMessage(content=f"Original query: {query}\n\nRefined query:"),
    ]
    try:
        llm = _build_llm(temperature=0.0, model_name=model_name)
        response = await llm.ainvoke(messages)
        refined = response.content.strip().strip('"')
        if refined and refined.lower() != query.lower():
            logger.info("Query refinement: '%s' → '%s'", query, refined)
            return refined
        return None
    except Exception as e:
        logger.warning("Query refinement failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Suggested prompts
# ---------------------------------------------------------------------------

async def generate_suggested_prompts(
    query: str,
    answer: str,
    conversation_history: list[dict] | None = None,
    model_name: str | None = None,
) -> list[str]:
    context_parts = []
    if conversation_history:
        for turn in conversation_history[-2:]:
            role = "User" if turn["role"] == "user" else "Assistant"
            content = turn["content"][:200]
            context_parts.append(f"{role}: {content}")

    context_parts.append(f"User: {query}")
    context_parts.append(f"Assistant: {answer[:500]}")
    context_text = "\n".join(context_parts)

    messages = [
        SystemMessage(content=SUGGESTED_PROMPTS_PROMPT),
        HumanMessage(content=f"Conversation:\n{context_text}\n\nSuggested follow-up questions:"),
    ]

    try:
        llm = _build_llm(temperature=0.7, model_name=model_name)
        response = await llm.ainvoke(messages)
        lines = [line.strip() for line in response.content.strip().split("\n") if line.strip()]
        suggestions = []
        for line in lines[:3]:
            cleaned = line.lstrip("0123456789.-•) ").strip()
            if cleaned:
                suggestions.append(cleaned)
        logger.info("Generated %d suggested prompts", len(suggestions))
        return suggestions
    except Exception as e:
        logger.warning("Suggested prompts generation failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Retrieval quality gate
# ---------------------------------------------------------------------------

def _log_retrieval_decision(
    query: str,
    original_score: float,
    retry_triggered: bool,
    refined_query: str | None,
    retry_score: float | None,
    decision: str,
) -> None:
    logger.info(
        "Retrieval: query=%r score=%.3f retry=%s refined=%r "
        "retry_score=%s decision=%s threshold=%.2f",
        query[:80],
        original_score,
        retry_triggered,
        (refined_query[:80] if refined_query else None),
        f"{retry_score:.3f}" if retry_score is not None else "n/a",
        decision,
        settings.retrieval_score_threshold,
    )


# ---------------------------------------------------------------------------
# Main invoke
# ---------------------------------------------------------------------------

async def invoke_agent(
    query: str,
    conversation_history: list[dict] | None = None,
    top_k: int | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
    model_name: str | None = None,
    filter_expr: str | None = None,
) -> AgentResult:
    """Invoke the RAG agent: search → deduplicate → generate → return structured result."""
    effective_top_k = top_k if top_k is not None else settings.default_top_k
    effective_prompt = system_prompt if system_prompt else SYSTEM_PROMPT

    if "sttm" in query.lower() and not system_prompt:
        effective_prompt = STTM_SYSTEM_PROMPT
        effective_top_k = max(effective_top_k, settings.sttm_top_k)

    search_query = query
    query_was_rewritten = False
    if conversation_history:
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)
        query_was_rewritten = search_query != query

    search_client = await get_search_client()

    raw_documents = await search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr)
    logger.info("Retrieved %d docs (top_k=%d) for: %s", len(raw_documents), effective_top_k, search_query[:100])
    documents = raw_documents

    if not documents:
        return AgentResult(answer=OUT_OF_SCOPE_ANSWER, sources=[], raw_chunks=[], full_prompt="")

    original_score = max(
            (d.get("reranker_score") or d.get("score", 0)) for d in documents
        )
        top_score = original_score
        retry_triggered = False
        refined_query = None
        retry_score = None
        decision = "passed"

        if settings.retrieval_score_threshold > 0 and top_score < settings.retrieval_score_threshold:
            retry_triggered = True
            refined_query = await _refine_query_for_retry(search_query, model_name)
            if refined_query:
                # Reranker should score against search_query (the best intent
                # before refinement), not the raw vague follow-up.
                retry_raw = await search_client.search(refined_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=search_query)
                if retry_raw:
                    retry_score = max(
                        (d.get("reranker_score") or d.get("score", 0)) for d in retry_raw
                    )
                    if retry_score >= top_score:
                        raw_documents = retry_raw
                        documents = retry_raw
                        search_query = refined_query
                        top_score = retry_score
                        decision = "improved_via_retry"
                    else:
                        decision = "retry_worse_fallback"

            if top_score < settings.retrieval_score_threshold:
                decision = "blocked"

        _log_retrieval_decision(search_query, original_score, retry_triggered, refined_query, retry_score, decision)

        if decision == "blocked":
            return AgentResult(answer=OUT_OF_SCOPE_ANSWER, sources=[], raw_chunks=raw_documents, full_prompt="")

    all_doc_names = None
    unique_sources = _get_unique_source_names(raw_documents)
    if len(unique_sources) > 1:
        all_doc_names = await search_client.search_document_names(search_query)

    context = _format_context(raw_documents, all_document_names=all_doc_names)
    sources = _build_sources(raw_documents)

    messages = [SystemMessage(content=effective_prompt)]
    if conversation_history:
        for turn in conversation_history[-6:]:
            if turn["role"] == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif turn["role"] == "assistant":
                messages.append(AIMessage(content=turn["content"]))

    user_prompt = f"""Question: {query}

Documents:
{context}

Answer:"""
    messages.append(HumanMessage(content=user_prompt))
    full_prompt = f"=== SYSTEM PROMPT ===\n{effective_prompt}\n\n=== USER PROMPT (with context) ===\n{user_prompt}"

    llm = _build_llm(temperature=temperature, model_name=model_name)
    try:
        response = await llm.ainvoke(messages)
        answer = response.content
        cited_sources = _filter_cited_sources(answer, sources)
        logger.info(
            "Generated answer: length=%d, model=%s, cited=%d/%d",
            len(answer), model_name or settings.azure_openai_deployment_name,
            len(cited_sources), len(sources),
        )
        return AgentResult(answer=answer, sources=cited_sources, raw_chunks=raw_documents, full_prompt=full_prompt)
    except Exception as e:
        raise GenerationError(f"LLM call failed: {e}") from e


# ---------------------------------------------------------------------------
# Streaming invoke
# ---------------------------------------------------------------------------

async def invoke_agent_stream(
    query: str,
    conversation_history: list[dict] | None = None,
    top_k: int | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
    model_name: str | None = None,
    filter_expr: str | None = None,
):
    """Streaming version — yields token strings and a final metadata dict."""
    effective_top_k = top_k if top_k is not None else settings.default_top_k
    effective_prompt = system_prompt if system_prompt else SYSTEM_PROMPT

    if "sttm" in query.lower() and not system_prompt:
        effective_prompt = STTM_SYSTEM_PROMPT
        effective_top_k = max(effective_top_k, settings.sttm_top_k)

    search_query = query
    query_was_rewritten = False
    if conversation_history:
        yield {"type": "event", "event": "rewriting_query"}
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)
        query_was_rewritten = search_query != query
        yield {"type": "event", "event": "query_rewritten", "query": search_query}

    yield {"type": "event", "event": "search_start"}

    search_client = await get_search_client()

    raw_documents = await search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr)
    documents = raw_documents

    if not documents:
        yield {"type": "event", "event": "search_complete", "sources": 0}
        yield {
            "type": "metadata",
            "answer": OUT_OF_SCOPE_ANSWER,
            "sources": [],
            "raw_chunks": [],
            "full_prompt": "",
            "search_query": search_query,
            "original_query": query,
            "query_rewritten": search_query != query,
        }
        return

    search_event_emitted = False
    original_score = max(
        (d.get("reranker_score") or d.get("score", 0)) for d in documents
    )
    top_score = original_score
    retry_triggered = False
    refined_query = None
    retry_score = None
    decision = "passed"

    if settings.retrieval_score_threshold > 0 and top_score < settings.retrieval_score_threshold:
        retry_triggered = True
        unique_src = _get_unique_source_names(documents)
        yield {"type": "event", "event": "search_complete", "sources": len(unique_src)}
        yield {"type": "event", "event": "refining_search"}

        refined_query = await _refine_query_for_retry(search_query, model_name)
        if refined_query:
            retry_raw = await search_client.search(refined_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=search_query)
            if retry_raw:
                retry_score = max(
                    (d.get("reranker_score") or d.get("score", 0)) for d in retry_raw
                )
                if retry_score >= top_score:
                    raw_documents = retry_raw
                    documents = retry_raw
                    search_query = refined_query
                    top_score = retry_score
                    decision = "improved_via_retry"
                else:
                    decision = "retry_worse_fallback"

        if top_score < settings.retrieval_score_threshold:
            decision = "blocked"

        _log_retrieval_decision(search_query, original_score, retry_triggered, refined_query, retry_score, decision)

        if retry_triggered and decision != "blocked":
            unique_src = _get_unique_source_names(documents)
            yield {"type": "event", "event": "retry_search_complete", "sources": len(unique_src)}
            search_event_emitted = True

        if decision == "blocked":
            yield {
                "type": "metadata",
                "answer": OUT_OF_SCOPE_ANSWER,
                "sources": [],
                "raw_chunks": raw_documents,
                "full_prompt": "",
                "search_query": search_query,
                "original_query": query,
                "query_rewritten": search_query != query,
            }
            return

    all_doc_names = None
    unique_sources = _get_unique_source_names(raw_documents)
    if len(unique_sources) > 1:
        all_doc_names = await search_client.search_document_names(search_query)

    if not search_event_emitted:
        yield {"type": "event", "event": "search_complete", "sources": len(unique_sources)}

    context = _format_context(raw_documents, all_document_names=all_doc_names)
    sources = _build_sources(raw_documents)

    messages = [SystemMessage(content=effective_prompt)]
    if conversation_history:
        for turn in conversation_history[-6:]:
            if turn["role"] == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif turn["role"] == "assistant":
                messages.append(AIMessage(content=turn["content"]))

    user_prompt = f"""Question: {query}

Documents:
{context}

Answer:"""
    messages.append(HumanMessage(content=user_prompt))
    full_prompt = f"=== SYSTEM PROMPT ===\n{effective_prompt}\n\n=== USER PROMPT (with context) ===\n{user_prompt}"

    yield {"type": "event", "event": "generating"}

    llm = _build_llm(temperature=temperature, model_name=model_name)
    answer_chunks = []
    try:
        async for chunk in llm.astream(messages):
            token = chunk.content
            if token:
                answer_chunks.append(token)
                yield token

        full_answer = "".join(answer_chunks)
        cited_sources = _filter_cited_sources(full_answer, sources)
        logger.info(
            "Streamed answer: length=%d, model=%s, cited=%d/%d",
            len(full_answer), model_name or settings.azure_openai_deployment_name,
            len(cited_sources), len(sources),
        )
        yield {
            "type": "metadata",
            "answer": full_answer,
            "sources": cited_sources,
            "raw_chunks": raw_documents,
            "full_prompt": full_prompt,
            "search_query": search_query,
            "original_query": query,
            "query_rewritten": search_query != query,
        }
    except Exception as e:
        logger.error("LLM stream failed: %s", e)
        yield "Sorry, I couldn't generate an answer right now. Please try again."
        yield {
            "type": "metadata",
            "answer": "Sorry, I couldn't generate an answer right now. Please try again.",
            "sources": [],
            "raw_chunks": [],
            "full_prompt": full_prompt,
            "search_query": search_query,
            "original_query": query,
            "query_rewritten": search_query != query,
        }
