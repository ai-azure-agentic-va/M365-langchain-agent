"""LangChain RAG agent — retrieves from Azure AI Search, generates citation-backed answers.

Pure LangChain (no LangGraph). Takes a user query + optional conversation history,
searches the configured index, and returns a grounded answer with inline citations.

Returns structured results: answer text + list of source documents with full metadata
(URLs, titles, page numbers, relevance scores, content previews).
"""

import logging
import os
import re
from collections import Counter
from typing import List, Dict, Optional, TypedDict
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from m365_langchain_agent.utils.search import get_search_client

logger = logging.getLogger(__name__)

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
- Say "This question appears to be outside the scope of the knowledge base. I can only answer based on available documentation."
- Do NOT make up an answer or hallucinate information.
- Do NOT say "the provided documents" — the user is not providing documents, the system is searching a knowledge base.

Multiple-source rules:
- When documents come from multiple source files, SYNTHESIZE a single answer using all relevant sources. Cite each source with its number.
- Only ask the user to clarify if the sources contain CONFLICTING information on the same topic and you cannot determine which is correct.
- Example of when to ask: Document [1] says the retention period is 30 days, but document [2] says 90 days — ask which policy applies.
- Example of when NOT to ask: Documents [1] and [2] both discuss refund policies from different angles — synthesize both into one answer.
- Never ask "which document?" simply because multiple documents were retrieved. The user expects a complete answer.

Greeting rules:
- If the user greeting is generic (for example, "hi", "hello", or "hey") and no specific information is being requested, do not use the retrieved documents. Instead respond with: "Hello! I'm the ETS Virtual Assistant. How can I help you today?"
- If the greeting includes a question (e.g. "Hi, what is the refund policy?"), answer the question normally using the documents.

Do not summarize or recap information you have already presented in the same answer.

Keep answers concise, well-structured, and focused on the question asked.
Use markdown formatting (bold, bullet points, headers) where it improves readability."""

STTM_SYSTEM_PROMPT = os.environ.get("STTM_SYSTEM_PROMPT", """This question should be answered using the Source-to-Target Mapping (STTM) Excel workbooks. STTM (Source-to-Target Mapping) document describes data lineage across a data platform. There are typically two Excel workbooks.

This first workbook describes how data moves across enterprise ingestion and transformation layers:
Landing → RAW → INT → CUR.
RAW to INT tabs describe field-level mappings, transformations, and standard metadata added during ingestion.
INT to CUR tabs describe final transformations, renaming, derivations, filtering, and curation decisions.
Presence flags (Y/N) indicate whether a field is propagated to the next layer.
Transformation Logic columns explain how derived fields are created.
PII/PCI flags indicate sensitive data handling requirements.
Source System Table Information captures the path for Raw, INT and CUR for each table.

The second workbook describes how curated data is moved from CUR to ASL (Azure Synapse Layer), which is the final serving layer consumed by downstream systems.
Each row represents a single target attribute with full lineage.
Source details include CUR file path, column name, data type, and constraints.
Target details include ASL table, column, datatype, nullability, and PK indicators.
Transformation logic is explicitly stated (e.g., Pass Through, Derived, ETL Generated, conditional logic).
Common patterns include snapshot-date-based deduplication, PK validation, and ETL-generated metadata (LOAD_TS, LOAD_PROC_NM).
Logic Type values define whether fields are pass-through, derived, lookup-based, or system-generated.
Target Column Is PK, Target Column Is FK suggest if the field is a primary key or Foreign key.

STTM workbooks are typically organized across multiple Excel tabs, often separated by but not limited to:
Data hop (e.g., Landing→RAW, RAW→INT, INT→CUR, CUR→ASL), document Info, source system info, target tables path or schemas.

When answering questions using STTM documents: identify the correct STTM workbook, identify the correct hop(s) and relevant tabs, retrieve and apply the appropriate mapping rules.
If the question requires end-to-end lineage, combine results across all applicable hops in the correct order.

Citation rules:
- Reference documents with numbered citations like [1], [2], etc.
- Place citations inline, right after the relevant claim.
- Never invent citations — only cite documents that are actually provided.

When the knowledge base does not contain relevant information:
- Say "The knowledge base does not contain enough information to answer that."
- Do NOT make up an answer or hallucinate information.

Keep answers precise and structured. Use markdown tables, bold, and bullet points for readability.

Output rules:
- Present lineage in a SINGLE pass — do not repeat information.
- If you show a table or hop-by-hop breakdown, do NOT follow it with a prose summary of the same data.
- End with caveats or notes if needed, not a recap.
- Prefer a single end-to-end markdown table over separate hop descriptions followed by a combined view.
- Only add explanatory prose when transformation logic is complex and needs clarification.""")

STTM_TOP_K = int(os.environ.get("STTM_TOP_K", "20"))


_STTM_KEYWORDS = frozenset({
    "sttm", "lineage", "source to target", "source-to-target",
    "raw to int", "int to cur", "cur to asl", "landing to raw",
    "data mapping", "field mapping", "column mapping",
    "hop", "transformation logic",
})


def _is_sttm_query(query: str) -> bool:
    """Check if the query is STTM-related (case-insensitive)."""
    q = query.lower()
    return any(kw in q for kw in _STTM_KEYWORDS)


_STTM_HOPS = [
    ("landing", "raw"),
    ("raw", "int"),
    ("int", "cur"),
    ("cur", "asl"),
]

_STTM_MULTIHOP_SIGNALS = frozenset({
    "end to end", "end-to-end", "all layers", "all hops", "through all",
    "across layers", "across hops", "full lineage", "complete lineage",
    "landing to asl", "raw to asl", "raw to cur", "landing to cur",
    "source to asl", "from source to target",
})


def _detect_sttm_hops(query: str) -> list[tuple[str, str]]:
    """Determine which STTM hops a query targets.

    Returns a list of (from_layer, to_layer) tuples.  An empty list means
    the query is STTM-related but not hop-specific — fall back to the
    normal broad search.
    """
    q = query.lower()

    explicit: list[tuple[str, str]] = []
    for src, tgt in _STTM_HOPS:
        patterns = [
            f"{src} to {tgt}",
            f"{src}-to-{tgt}",
            f"{src} → {tgt}",
            f"{src}->{tgt}",
            f"{src}→{tgt}",
        ]
        if any(p in q for p in patterns):
            explicit.append((src, tgt))

    if explicit:
        return explicit

    if any(signal in q for signal in _STTM_MULTIHOP_SIGNALS):
        return list(_STTM_HOPS)

    return []


def _sttm_hop_search(
    search_client,
    base_query: str,
    hops: list[tuple[str, str]],
    top_k_per_hop: int = 8,
    original_query: str = None,
) -> list[dict]:
    """Run targeted searches for each STTM hop and merge results.

    Each hop search appends layer terms to the base query so the embedding
    and keyword components both target that specific transition.  Results
    are merged and deduplicated while preserving chunk diversity across hops
    (relaxed dedup: keeps multiple chunks per source document).

    Args:
        search_client: The AzureSearchClient singleton.
        base_query: The user's search query (possibly rewritten).
        hops: List of (from_layer, to_layer) tuples to search.
        top_k_per_hop: Chunks to retrieve per hop search.

    Returns:
        Merged, deduplicated list of document dicts.
    """
    all_chunks: list[dict] = []
    seen_ids: set[str] = set()

    # Reranker should score against the user's original intent, not the
    # hop-augmented search_text (e.g. "query raw to int").
    reranker_query = original_query or base_query

    for src, tgt in hops:
        hop_query = f"{base_query} {src} to {tgt}"
        try:
            results = search_client.search(hop_query, top_k=top_k_per_hop, semantic_query=reranker_query)
            new_count = 0
            for doc in results:
                doc_id = doc.get("content", "")[:200]
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_chunks.append(doc)
                    new_count += 1
            logger.info(
                f"[Agent] STTM hop search {src}→{tgt}: "
                f"{len(results)} retrieved, {new_count} new after dedup"
            )
        except Exception as e:
            logger.warning(f"[Agent] STTM hop search {src}→{tgt} failed: {e}")

    all_chunks.sort(
        key=lambda d: (d.get("reranker_score") or 0, d.get("score", 0)),
        reverse=True,
    )

    logger.info(f"[Agent] STTM hop search complete: {len(all_chunks)} total chunks from {len(hops)} hops")
    return all_chunks


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

RETRIEVAL_SCORE_THRESHOLD = float(os.environ.get("RETRIEVAL_SCORE_THRESHOLD", "1.2"))

_OUT_OF_SCOPE_ANSWER = (
    "This question appears to be outside the scope of the knowledge base. "
    "I can only answer based on available documentation. "
    "Try rephrasing your question or asking about a specific topic covered in the knowledge base."
)


def _is_reasoning_model(deployment: str) -> bool:
    """Check if a deployment is a reasoning model (o1, o3, etc.) that doesn't support temperature."""
    return deployment.startswith("o3") or deployment.startswith("o1")


def _build_llm(temperature: float = None, model_name: str = None) -> AzureChatOpenAI:
    """Create the Azure OpenAI LLM client with configurable parameters."""
    deployment = model_name or DEFAULT_MODEL
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



def _extract_section_label(content: str) -> Optional[str]:
    """Extract a meaningful section/sheet label from chunk content.

    Priority:
      1. Excel sheet name  — "Sheet: <name>", "Worksheet: <name>", "Tab: <name>"
      2. Markdown heading   — first # or ## heading in the content
      3. None               — no meaningful label found; caller uses base filename
    """
    # Excel sheet patterns (case-insensitive, anywhere in first 500 chars)
    head = content[:500]
    sheet_match = re.search(
        r"(?:Sheet|Worksheet|Tab)\s*:\s*(.+?)(?:\n|$)", head, re.IGNORECASE
    )
    if sheet_match:
        name = sheet_match.group(1).strip()
        if name:
            return f"Sheet: {name}"

    # Markdown heading (first # or ## line)
    heading_match = re.search(r"^#{1,2}\s+(.+)", content, re.MULTILINE)
    if heading_match:
        heading = heading_match.group(1).strip()
        if heading:
            return f"Section: {heading}"

    return None


def _build_sources(documents: List[Dict]) -> List[Source]:
    """Convert raw search results into Source dicts with preview text.

    When multiple chunks share the same base title (e.g. same Excel file but
    different sheets), appends a human-friendly differentiator:
      - Sheet name for Excel files
      - Section heading for markdown/docs
      - Base filename only as fallback (never Part X/Y or chunk indices)
    """
    # First pass: detect which base titles appear more than once
    base_titles = []
    for d in documents:
        base_titles.append(d.get("document_title") or d.get("file_name") or "Untitled")
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

        # Build a unique display title for duplicates
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


SUGGESTED_PROMPTS_PROMPT = """Based on the conversation so far, suggest exactly 3 short follow-up questions the user might want to ask next.

Rules:
- Each question must be self-contained (don't use pronouns like "it" or "that").
- Questions should explore different angles: deeper detail, related topics, or comparisons.
- Keep each question under 15 words.
- Return ONLY 3 lines, one question per line. No numbering, no bullets, no extra text."""


async def generate_suggested_prompts(
    query: str,
    answer: str,
    conversation_history: Optional[List[Dict]] = None,
    model_name: str = None,
) -> List[str]:
    """Generate 3 follow-up question suggestions based on the conversation.

    Uses a lightweight LLM call (temperature=0.7 for variety) with minimal context
    to keep cost low (~200 tokens total).

    Returns:
        List of 3 suggested follow-up questions, or empty list on failure.
    """
    # Build compact context: last exchange + current Q&A
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
        suggestion_model = model_name or DEFAULT_MODEL
        llm = _build_llm(temperature=0.7, model_name=suggestion_model)
        response = await llm.ainvoke(messages)
        lines = [line.strip() for line in response.content.strip().split("\n") if line.strip()]
        suggestions = []
        for line in lines[:3]:
            # Remove leading "1.", "- ", "• " etc.
            cleaned = line.lstrip("0123456789.-•) ").strip()
            if cleaned:
                suggestions.append(cleaned)
        logger.info(f"[Agent] Generated {len(suggestions)} suggested prompts")
        return suggestions
    except Exception as e:
        logger.warning(f"[Agent] Suggested prompts generation failed: {e}")
        return []


QUERY_REWRITE_PROMPT = """Given the conversation history and a follow-up question, rewrite the follow-up question as a standalone search query that captures the full intent.

Rules:
- If the follow-up is already self-contained, return it as-is.
- Do NOT answer the question — only rewrite it.
- Keep the rewritten query concise (under 30 words).
- Return ONLY the rewritten query, nothing else."""

QUERY_REFINE_PROMPT = """The following search query returned low-relevance results from a knowledge base. Rewrite it to improve retrieval. The knowledge base contains enterprise documents, data mappings, policies, and technical guides.

Rules:
- Broaden the query if it is too specific, or add synonyms / related terms.
- If the query uses abbreviations, expand them.
- Do NOT answer the question — only rewrite the search query.
- Keep it concise (under 30 words).
- Return ONLY the rewritten query, nothing else."""


async def _refine_query_for_retry(
    query: str,
    model_name: str = None,
) -> str | None:
    """Refine a query that produced low-relevance results.

    Returns a refined query string, or None if refinement fails or
    produces the same query.
    """
    messages = [
        SystemMessage(content=QUERY_REFINE_PROMPT),
        HumanMessage(content=f"Original query: {query}\n\nRefined query:"),
    ]
    try:
        llm = _build_llm(temperature=0.0, model_name=model_name)
        response = await llm.ainvoke(messages)
        refined = response.content.strip().strip('"')
        if refined and refined.lower() != query.lower():
            logger.info(f"[Agent] Query refinement: '{query}' → '{refined}'")
            return refined
        logger.info("[Agent] Query refinement produced same query — skipping retry")
        return None
    except Exception as e:
        logger.warning(f"[Agent] Query refinement failed: {e}")
        return None


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


def _extract_logical_path(source_url: str) -> Optional[str]:
    """Extract the human-readable logical path from a blob storage URL.

    Strips the blob host and container, returning only the meaningful
    folder/file hierarchy.  Example:
        https://acct.blob.core.windows.net/container/NFCU-VA-WIKI/Release/PayGuard.md
        → NFCU-VA-WIKI/Release/PayGuard.md

    Returns None if the URL is empty or has no path beyond the container.
    """
    if not source_url or not source_url.strip():
        return None
    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(source_url)
        # path looks like /container/rest/of/path
        segments = parsed.path.strip("/").split("/", 1)
        if len(segments) < 2 or not segments[1]:
            return None
        return unquote(segments[1])
    except Exception:
        return None


def _format_context(documents: List[Dict], all_document_names: List[str] = None) -> str:
    """Format retrieved documents into numbered context for the LLM.

    Args:
        documents: Deduplicated search results with content.
        all_document_names: Optional complete list of matching document names
            from facet-based discovery. When provided, the disambiguation hint
            includes ALL matching docs, not just the retrieved subset.
    """
    if not documents:
        return "No documents found."

    unique_sources = _get_unique_source_names(documents)
    hint = ""
    if len(unique_sources) > 1:
        # Use the full facet-based list if available; fall back to retrieved set
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


def _log_retrieval_decision(
    query: str,
    original_score: float,
    retry_triggered: bool,
    refined_query: Optional[str],
    retry_score: Optional[float],
    decision: str,
) -> None:
    """Emit a single structured log line summarising the retrieval quality gate."""
    logger.info(
        "[Retrieval] query=%r original_score=%.3f retry=%s refined_query=%r "
        "retry_score=%s decision=%s threshold=%.2f",
        query[:80],
        original_score,
        retry_triggered,
        (refined_query[:80] if refined_query else None),
        f"{retry_score:.3f}" if retry_score is not None else "n/a",
        decision,
        RETRIEVAL_SCORE_THRESHOLD,
    )


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

    is_sttm = _is_sttm_query(query)
    if is_sttm and not system_prompt:
        effective_prompt = STTM_SYSTEM_PROMPT
        effective_top_k = max(effective_top_k, STTM_TOP_K)
        logger.info(f"[Agent] STTM query detected — using STTM prompt, top_k={effective_top_k}")

    search_query = query
    query_was_rewritten = False
    if conversation_history:
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)
        query_was_rewritten = search_query != query

    search_client = get_search_client()

    # semantic_query tells the reranker what to score against.
    # - History rewrite: rewritten query IS the enriched intent → let reranker
    #   use search_text (None). The raw follow-up is often too vague.
    # - STTM hop augmentation: appended layer terms are noise → use original query.
    # - First turn / no rewrite: search_text == query → None (no override needed).
    reranker_query = None  # default: reranker uses search_text

    sttm_hops = _detect_sttm_hops(query) if is_sttm else []
    if sttm_hops:
        logger.info(f"[Agent] STTM hop-by-hop mode: {len(sttm_hops)} hops detected")
        raw_documents = _sttm_hop_search(search_client, search_query, sttm_hops, original_query=query)
        documents = raw_documents
    else:
        raw_documents = search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=reranker_query)
        logger.info(f"[Agent] Retrieved {len(raw_documents)} docs (top_k={effective_top_k}, sttm={is_sttm}) for: {search_query[:100]}")
        documents = raw_documents

    if not documents:
        logger.info("[Agent] Quality gate: zero results — skipping LLM")
        return AgentResult(
            answer=_OUT_OF_SCOPE_ANSWER, sources=[], raw_chunks=[], full_prompt=""
        )

    if not sttm_hops:
        original_score = max(
            (d.get("reranker_score") or d.get("score", 0)) for d in documents
        )
        top_score = original_score
        retry_triggered = False
        refined_query = None
        retry_score = None
        decision = "passed"

        if RETRIEVAL_SCORE_THRESHOLD > 0 and top_score < RETRIEVAL_SCORE_THRESHOLD:
            retry_triggered = True
            refined_query = await _refine_query_for_retry(search_query, model_name)
            if refined_query:
                # Reranker should score against search_query (the best intent
                # before refinement), not the raw vague follow-up.
                retry_raw = search_client.search(refined_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=search_query)
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

            if top_score < RETRIEVAL_SCORE_THRESHOLD:
                decision = "blocked"

        _log_retrieval_decision(search_query, original_score, retry_triggered, refined_query, retry_score, decision)

        if decision == "blocked":
            return AgentResult(
                answer=_OUT_OF_SCOPE_ANSWER, sources=[], raw_chunks=raw_documents, full_prompt=""
            )

    all_doc_names = None
    unique_sources = _get_unique_source_names(raw_documents)
    if len(unique_sources) > 1:
        all_doc_names = search_client.search_document_names(search_query)
        if all_doc_names:
            logger.info(
                f"[Agent] Document discovery: {len(all_doc_names)} total docs "
                f"(retrieved {len(unique_sources)})"
            )

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

    is_sttm = _is_sttm_query(query)
    if is_sttm and not system_prompt:
        effective_prompt = STTM_SYSTEM_PROMPT
        effective_top_k = max(effective_top_k, STTM_TOP_K)
        logger.info(f"[Agent] STTM query detected — using STTM prompt, top_k={effective_top_k}")

    search_query = query
    query_was_rewritten = False
    if conversation_history:
        yield {"type": "event", "event": "rewriting_query"}
        search_query = await _rewrite_query_with_history(query, conversation_history, model_name)
        query_was_rewritten = search_query != query
        yield {"type": "event", "event": "query_rewritten", "query": search_query}

    yield {"type": "event", "event": "search_start"}

    search_client = get_search_client()
    reranker_query = None  # rewritten query IS the enriched intent; don't override

    sttm_hops = _detect_sttm_hops(query) if is_sttm else []
    if sttm_hops:
        logger.info(f"[Agent] STTM hop-by-hop mode: {len(sttm_hops)} hops detected")
        raw_documents = _sttm_hop_search(search_client, search_query, sttm_hops, original_query=query)
        documents = raw_documents
    else:
        raw_documents = search_client.search(search_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=reranker_query)
        documents = raw_documents

    if not documents:
        logger.info("[Agent] Quality gate: zero results — skipping LLM")
        yield {"type": "event", "event": "search_complete", "sources": 0}
        yield {
            "type": "metadata",
            "answer": _OUT_OF_SCOPE_ANSWER,
            "sources": [],
            "raw_chunks": [],
            "full_prompt": "",
            "search_query": search_query,
            "original_query": query,
            "query_rewritten": search_query != query,
        }
        return

    search_event_emitted = False
    if not sttm_hops:
        original_score = max(
            (d.get("reranker_score") or d.get("score", 0)) for d in documents
        )
        top_score = original_score
        retry_triggered = False
        refined_query = None
        retry_score = None
        decision = "passed"

        if RETRIEVAL_SCORE_THRESHOLD > 0 and top_score < RETRIEVAL_SCORE_THRESHOLD:
            retry_triggered = True
            unique_sources = _get_unique_source_names(documents)
            yield {"type": "event", "event": "search_complete", "sources": len(unique_sources)}
            yield {"type": "event", "event": "refining_search"}

            refined_query = await _refine_query_for_retry(search_query, model_name)
            if refined_query:
                retry_raw = search_client.search(refined_query, top_k=effective_top_k, filter_expr=filter_expr, semantic_query=search_query)
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

            if top_score < RETRIEVAL_SCORE_THRESHOLD:
                decision = "blocked"

        _log_retrieval_decision(search_query, original_score, retry_triggered, refined_query, retry_score, decision)

        if retry_triggered and decision != "blocked":
            unique_sources = _get_unique_source_names(documents)
            yield {"type": "event", "event": "retry_search_complete", "sources": len(unique_sources)}
            search_event_emitted = True

        if decision == "blocked":
            yield {
                "type": "metadata",
                "answer": _OUT_OF_SCOPE_ANSWER,
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
        all_doc_names = search_client.search_document_names(search_query)

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
            f"[Agent] Streamed answer, length={len(full_answer)}, model={model_name or DEFAULT_MODEL}, "
            f"cited={len(cited_sources)}/{len(sources)} sources"
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
        logger.error(f"[Agent] LLM stream failed: {e}")
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


def _filter_cited_sources(answer: str, sources: List[Source]) -> List[Source]:
    """Return only sources that the LLM actually cited in its answer.

    Parses [1], [2], etc. from the answer text. If no citations are found,
    returns all sources as fallback (the LLM may have used prose references).
    """
    cited_indices = set(int(m) for m in re.findall(r"\[(\d{1,2})\]", answer))
    if not cited_indices:
        return sources
    filtered = [s for s in sources if s.get("index") in cited_indices]
    return filtered if filtered else sources


def format_sources_markdown(sources: List[Source]) -> str:
    """Format sources as markdown with clickable links — deduplicated by file name."""
    if not sources:
        return ""

    lines = []
    seen_names = set()
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
