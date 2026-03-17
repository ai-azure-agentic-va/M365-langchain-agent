"""LangChain RAG agent — retrieves from Azure AI Search, generates citation-backed answers.

Pure LangChain (no LangGraph). Takes a user query + optional conversation history,
searches the configured index, and returns a grounded answer with inline citations.

Returns structured results: answer text + list of source documents with full metadata
(URLs, titles, page numbers, relevance scores, content previews).
"""

import logging
import os
from typing import List, Dict, Optional, TypedDict

from dotenv import load_dotenv

load_dotenv()

from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from m365_langchain_agent.utils.search import get_search_client

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
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
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

    If the URL is an ADLS blob path (from ingestion before sidecar fix),
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


def _format_context(documents: List[Dict]) -> str:
    """Format retrieved documents into numbered context for the LLM."""
    if not documents:
        return "No documents found."

    parts = []
    for i, d in enumerate(documents):
        title = d.get("document_title") or d.get("file_name") or "Untitled"
        parts.append(f"[{i+1}] (Source: {title})\n{d['content']}")

    return "\n\n".join(parts)


async def invoke_agent(
    query: str,
    conversation_history: Optional[List[Dict]] = None,
    top_k: int = None,
    temperature: float = None,
    system_prompt: str = None,
    model_name: str = None,
) -> AgentResult:
    """Invoke the RAG agent: search → deduplicate → generate → return structured result.

    Args:
        query: The user's question.
        conversation_history: Optional prior turns [{"role": "user"|"assistant", "content": "..."}].
        top_k: Number of chunks to retrieve (default: DEFAULT_TOP_K).
        temperature: LLM temperature (default: DEFAULT_TEMPERATURE).
        system_prompt: Override system prompt (default: SYSTEM_PROMPT).
        model_name: Azure OpenAI deployment name (default: DEFAULT_MODEL).

    Returns:
        AgentResult with 'answer', 'sources', and 'raw_chunks'.
    """
    effective_top_k = top_k if top_k is not None else DEFAULT_TOP_K
    effective_prompt = system_prompt if system_prompt else SYSTEM_PROMPT

    # 1. Retrieve documents from Azure AI Search
    search_client = get_search_client()
    raw_documents = search_client.search(query, top_k=effective_top_k)
    logger.info(f"[Agent] Retrieved {len(raw_documents)} docs (top_k={effective_top_k}) for: {query[:100]}")

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
        logger.info(f"[Agent] Generated answer, length={len(answer)}, model={model_name or DEFAULT_MODEL}")
        return AgentResult(answer=answer, sources=sources, raw_chunks=raw_documents, full_prompt=full_prompt)
    except Exception as e:
        logger.error(f"[Agent] LLM call failed: {e}")
        return AgentResult(
            answer="Sorry, I couldn't generate an answer right now. Please try again.",
            sources=[],
            raw_chunks=[],
            full_prompt=full_prompt,
        )


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
