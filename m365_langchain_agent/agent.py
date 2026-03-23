"""LangChain RAG agent — retrieves from Azure AI Search, generates citation-backed answers.

Pure LangChain (no LangGraph). Takes a user query + optional conversation history,
searches the configured index, and returns a grounded answer with inline citations.

Returns structured results: answer text + list of source documents with full metadata
(URLs, titles, page numbers, relevance scores, content previews).
"""

import logging
import os
import re
from typing import List, Dict, Optional, TypedDict

from dotenv import load_dotenv

load_dotenv()

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from m365_langchain_agent.utils.search import get_search_client

logger = logging.getLogger(__name__)

# Shared credential + token provider for Azure OpenAI (Managed Identity)
_credential = DefaultAzureCredential()
_token_provider = get_bearer_token_provider(_credential, "https://cognitiveservices.azure.com/.default")


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
    sources: List[Source]
    raw_chunks: List[Dict]
    full_prompt: str


# System prompt — instructs GPT-4.1 to use numbered citations that match source indices
SYSTEM_PROMPT = """You are a helpful assistant that answers questions using the provided documents from the knowledge base.

Citation rules:
- Reference documents with numbered citations like [1], [2], etc.
- Place citations inline, right after the relevant sentence or claim.
- If multiple documents support a point, combine them: [1][3].
- Never invent citations — only cite documents that are actually provided.

When the knowledge base does not contain relevant information:
- Say "The knowledge base does not contain enough information to answer that."
- Do NOT make up an answer or hallucinate information.
- Do NOT say "the provided documents" — the user is not providing documents, the system is searching a knowledge base.

Disambiguation rules:
- If the retrieved documents come from MULTIPLE distinct source files and the question does not specify which one, DO NOT blend answers from all of them.
- Instead, ask the user to clarify which document they are interested in.
- List the available documents by name so the user can pick.
- Example: "I found information in several documents: **[1] payments_sttm_workbook.xlsx**, **[2] logistics_sttm_workbook.xlsx**, and **[3] customer_sttm_workbook.xlsx**. Which one would you like me to answer from?"
- If the question clearly targets a specific topic or document (e.g. "payments STTM"), answer directly from the matching document without asking.
- If there is only one source document, answer directly.

Greeting rules:
- If the user greeting is generic (for example, "hi", "hello", or "hey") and no specific information is being requested, do not use the retrieved documents. Instead respond with: "Hello! I'm the ETS Virtual Assistant. How can I help you today?"
- If the greeting includes a question (e.g. "Hi, what is the refund policy?"), answer the question normally using the documents.

Keep answers concise, well-structured, and focused on the question asked.
Use markdown formatting (bold, bullet points, headers) where it improves readability."""


def get_available_models() -> list:
    """Return list of available model deployment names from env or defaults."""
    models_str = os.environ.get("AZURE_OPENAI_AVAILABLE_MODELS", "")
    if models_str.strip():
        return [m.strip() for m in models_str.split(",") if m.strip()]
    default = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1")
    defaults = [default]
    for m in ["gpt-4.1", "gpt-4.1-mini", "o3-mini"]:
        if m not in defaults:
            defaults.append(m)
    return defaults


DEFAULT_TOP_K = int(os.environ.get("DEFAULT_TOP_K", "5"))
DEFAULT_TEMPERATURE = float(os.environ.get("DEFAULT_TEMPERATURE", "0.2"))
DEFAULT_MODEL = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1")


def _is_reasoning_model(deployment: str) -> bool:
    """Check if a deployment is a reasoning model (o1, o3, etc.) that doesn't support temperature."""
    return deployment.startswith("o3") or deployment.startswith("o1")


def _build_llm(temperature: float = None, model_name: str = None) -> AzureChatOpenAI:
    """Create the Azure OpenAI LLM client with configurable parameters."""
    deployment = model_name or DEFAULT_MODEL
    # Reasoning models (o1, o3) require a newer API version and don't support temperature
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-05-01-preview")
    is_reasoning = _is_reasoning_model(deployment)
    if is_reasoning:
        api_version = "2024-12-01-preview"
    kwargs = dict(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        azure_ad_token_provider=_token_provider,
        azure_deployment=deployment,
        api_version=api_version,
    )
    if not is_reasoning:
        kwargs["temperature"] = temperature if temperature is not None else DEFAULT_TEMPERATURE
    return AzureChatOpenAI(**kwargs)


def _deduplicate_sources(documents: List[Dict]) -> List[Dict]:
    """Group chunks from the same document — keep the highest-scoring chunk per source_url."""
    seen = {}
    for d in documents:
        key = d.get("source_url") or d.get("file_name") or d.get("document_title") or id(d)
        existing = seen.get(key)
        if existing is None:
            seen[key] = d
        else:
            # Keep the one with higher reranker_score (or search score)
            new_score = d.get("reranker_score") or d.get("score", 0)
            old_score = existing.get("reranker_score") or existing.get("score", 0)
            if new_score > old_score:
                seen[key] = d
    return list(seen.values())


def _normalize_source_url(url: str, source_type: str) -> str:
    """Normalize source URLs — convert ADLS blob URLs to original source paths.

    If the URL is an ADLS blob path from ingestion,
    extract a clean relative path. If it's already a proper SharePoint/wiki URL,
    return as-is.
    """
    if not url:
        return ""
    # Already a proper HTTP URL (SharePoint, wiki, etc.) — keep as-is
    if url.startswith("http") and ".blob.core.windows.net" not in url:
        return url
    # ADLS blob URL — extract the meaningful path after the container
    if ".blob.core.windows.net" in url:
        # Pattern: https://<account>.blob.core.windows.net/<container>/<path>
        parts = url.split(".blob.core.windows.net/", 1)
        if len(parts) == 2:
            blob_path = parts[1]
            # Strip container prefix (e.g. "raw-documents/")
            segments = blob_path.split("/", 1)
            if len(segments) == 2:
                return segments[1]  # Return relative path without container
            return blob_path
    # Relative SharePoint path (e.g. /sites/...) — keep as-is
    return url


def _build_sources(documents: List[Dict]) -> List[Source]:
    """Convert raw search results into Source dicts with preview text."""
    sources = []
    for i, d in enumerate(documents):
        content = d.get("content", "")
        preview = content[:200].strip()
        if len(content) > 200:
            preview += "..."

        sources.append(Source(
            index=i + 1,
            title=d.get("document_title") or d.get("file_name") or "Untitled",
            url=_normalize_source_url(d.get("source_url", ""), d.get("source_type", "")),
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


QUERY_REWRITE_PROMPT = """Given the conversation history and a follow-up question, rewrite the follow-up question as a standalone search query that captures the full intent.

Rules:
- If the follow-up is already self-contained, return it as-is.
- Do NOT answer the question — only rewrite it.
- Keep the rewritten query concise (under 30 words).
- Return ONLY the rewritten query, nothing else."""


async def _rewrite_query_with_history(
    query: str,
    conversation_history: List[Dict],
    model_name: str = None,
) -> str:
    """Rewrite a follow-up question into a standalone search query using conversation context.

    Example:
        History: "What is the refund policy?" → "Refunds are processed within 30 days..."
        Follow-up: "How long does it take?"
        Rewritten: "How long does the refund policy take to process?"
    """
    if not conversation_history:
        return query

    # Build a compact history summary (last 2 exchanges)
    history_lines = []
    for turn in conversation_history[-4:]:
        role = "User" if turn["role"] == "user" else "Assistant"
        # Truncate long assistant responses to save tokens
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
        logger.info(f"[Agent] Query rewrite: '{query}' → '{rewritten}'")
        return rewritten
    except Exception as e:
        logger.warning(f"[Agent] Query rewrite failed, using original: {e}")
        return query


def _get_unique_source_names(documents: List[Dict]) -> List[str]:
    """Extract unique source file names from documents, preserving order."""
    seen = set()
    names = []
    for d in documents:
        name = d.get("document_title") or d.get("file_name") or "Untitled"
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _format_context(documents: List[Dict]) -> str:
    """Format retrieved documents into numbered context for the LLM."""
    if not documents:
        return "No documents found."

    # Add disambiguation hint when results span multiple source files
    unique_sources = _get_unique_source_names(documents)
    hint = ""
    if len(unique_sources) > 1:
        source_list = ", ".join(f'"{s}"' for s in unique_sources)
        hint = (
            f"NOTE: These documents come from {len(unique_sources)} distinct sources: "
            f"{source_list}. If the user's question is ambiguous about which source "
            f"they mean, ask them to clarify before answering.\n\n"
        )

    parts = []
    for i, d in enumerate(documents):
        title = d.get("document_title") or d.get("file_name") or "Untitled"
        parts.append(f"[{i+1}] (Source: {title})\n{d['content']}")

    return hint + "\n\n".join(parts)


async def invoke_agent(
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    top_k: int = None,
    temperature: float = None,
    system_prompt: str = None,
    model_name: str = None,
    filter_expr: str = None,
) -> AgentResult:
    """Invoke the RAG agent: search → deduplicate → generate → return structured result.

    Args:
        query: The user's question.
        conversation_history: Optional prior turns [{"role": "user"|"assistant", "content": "..."}].
        top_k: Number of chunks to retrieve (default: DEFAULT_TOP_K).
        temperature: LLM temperature (default: DEFAULT_TEMPERATURE).
        system_prompt: Override system prompt (default: SYSTEM_PROMPT).
        model_name: Azure OpenAI deployment name (default: DEFAULT_MODEL).
        filter_expr: Optional OData filter for metadata filtering (e.g. "file_name eq 'policy.pdf'").

    Returns:
        AgentResult with 'answer', 'sources', and 'raw_chunks'.
    """
    effective_top_k = top_k if top_k is not None else DEFAULT_TOP_K
    effective_prompt = system_prompt if system_prompt else SYSTEM_PROMPT

    # 0. Rewrite query using conversation context (for follow-up questions)
    search_query = query
    if conversation_history:
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)

    # 1. Retrieve documents from Azure AI Search (using rewritten query)
    search_client = get_search_client()
    raw_documents = search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr)
    logger.info(f"[Agent] Retrieved {len(raw_documents)} docs (top_k={effective_top_k}) for: {search_query[:100]}")

    # 2. Deduplicate — group chunks from same document
    documents = _deduplicate_sources(raw_documents)
    logger.info(f"[Agent] After dedup: {len(documents)} unique sources")

    # 3. Build context and sources
    context = _format_context(documents)
    sources = _build_sources(documents)

    # 4. Build message history for the LLM
    messages = [SystemMessage(content=effective_prompt)]

    if conversation_history:
        for turn in conversation_history[-6:]:  # Last 3 exchanges max
            if turn["role"] == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif turn["role"] == "assistant":
                messages.append(AIMessage(content=turn["content"]))

    user_prompt = f"""Question: {query}

Documents:
{context}

Answer:"""
    messages.append(HumanMessage(content=user_prompt))

    # Capture the full prompt for debug visibility
    full_prompt = f"=== SYSTEM PROMPT ===\n{effective_prompt}\n\n=== USER PROMPT (with context) ===\n{user_prompt}"

    # 5. Generate answer
    llm = _build_llm(temperature=temperature, model_name=model_name)
    try:
        response = await llm.ainvoke(messages)
        answer = response.content
        # Only return sources that the LLM actually cited in its answer
        cited_sources = _filter_cited_sources(answer, sources)
        logger.info(
            f"[Agent] Generated answer, length={len(answer)}, model={model_name or DEFAULT_MODEL}, "
            f"cited={len(cited_sources)}/{len(sources)} sources"
        )
        return AgentResult(answer=answer, sources=cited_sources, raw_chunks=raw_documents, full_prompt=full_prompt)
    except Exception as e:
        logger.error(f"[Agent] LLM call failed: {e}")
        return AgentResult(
            answer="Sorry, I couldn't generate an answer right now. Please try again.",
            sources=[],
            raw_chunks=[],
            full_prompt=full_prompt,
        )


async def invoke_agent_stream(
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    top_k: int = None,
    temperature: float = None,
    system_prompt: str = None,
    model_name: str = None,
    filter_expr: str = None,
):
    """Streaming version of invoke_agent — yields token strings as they arrive.

    After all tokens are yielded, yields a final dict with metadata:
    {"sources": [...], "raw_chunks": [...], "full_prompt": "...", "answer": "..."}
    """
    effective_top_k = top_k if top_k is not None else DEFAULT_TOP_K
    effective_prompt = system_prompt if system_prompt else SYSTEM_PROMPT

    search_query = query
    if conversation_history:
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)

    search_client = get_search_client()
    raw_documents = search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr)
    documents = _deduplicate_sources(raw_documents)
    context = _format_context(documents)
    sources = _build_sources(documents)

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
            f"[Agent] Streamed answer, length={len(full_answer)}, model={model_name or DEFAULT_MODEL}, "
            f"cited={len(cited_sources)}/{len(sources)} sources"
        )
        yield {
            "type": "metadata",
            "answer": full_answer,
            "sources": cited_sources,
            "raw_chunks": raw_documents,
            "full_prompt": full_prompt,
        }
    except Exception as e:
        logger.error(f"[Agent] LLM stream failed: {e}")
        yield "Sorry, I couldn't generate an answer right now. Please try again."
        yield {
            "type": "metadata",
            "answer": "Sorry, I couldn't generate an answer right now. Please try again.",
            "sources": [],
            "raw_chunks": [],
            "full_prompt": full_prompt,
        }


def _filter_cited_sources(answer: str, sources: List[Source]) -> List[Source]:
    """Return only sources that the LLM actually cited in its answer.

    Parses [1], [2], etc. from the answer text. If no citations are found,
    returns all sources as fallback (the LLM may have used prose references).
    """
    # Only match small numbers [1]-[99] to avoid false positives like [401] tax codes
    cited_indices = set(int(m) for m in re.findall(r"\[(\d{1,2})\]", answer))
    if not cited_indices:
        return sources
    filtered = [s for s in sources if s.get("index") in cited_indices]
    # If parsed indices didn't match any actual source, fall back to all sources
    return filtered if filtered else sources


def format_sources_markdown(sources: List[Source]) -> str:
    """Format sources as markdown with clickable links — used by bot.py and as fallback."""
    if not sources:
        return ""

    lines = []
    for s in sources:
        title = s.get("title", "Untitled")
        url = s.get("url", "")
        page = s.get("page_number", 0)
        score = s.get("reranker_score") or s.get("score", 0)

        if url:
            link = f"[{title}]({url})"
        else:
            link = f"**{title}**"

        parts = [f"[{s['index']}] {link}"]
        if page and page > 0:
            parts.append(f"p.{page}")
        if score and score > 0:
            parts.append(f"relevance: {score:.2f}")

        lines.append(" — ".join(parts))

    return "\n".join(lines)
