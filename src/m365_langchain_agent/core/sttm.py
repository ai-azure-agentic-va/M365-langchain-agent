"""STTM (Source-to-Target Mapping) query detection and hop-by-hop search.

Detects when a user query targets data lineage documents and routes
the search through specific data hops (Landing→RAW→INT→CUR→ASL)
for higher recall on multi-hop lineage questions.
"""

import logging

logger = logging.getLogger(__name__)

_STTM_KEYWORDS = frozenset({
    "sttm", "lineage", "source to target", "source-to-target",
    "raw to int", "int to cur", "cur to asl", "landing to raw",
    "data mapping", "field mapping", "column mapping",
    "hop", "transformation logic",
})

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


def is_sttm_query(query: str) -> bool:
    """Check if the query is STTM-related (case-insensitive)."""
    q = query.lower()
    return any(kw in q for kw in _STTM_KEYWORDS)


def detect_hops(query: str) -> list[tuple[str, str]]:
    """Determine which STTM hops a query targets.

    Returns a list of (from_layer, to_layer) tuples. An empty list means
    the query is STTM-related but not hop-specific — fall back to broad search.
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


async def hop_search(
    search_client,
    base_query: str,
    hops: list[tuple[str, str]],
    top_k_per_hop: int = 8,
    original_query: str | None = None,
) -> list[dict]:
    """Run targeted searches for each STTM hop and merge results.

    Each hop search appends layer terms to the base query so the embedding
    and keyword components both target that specific transition. Results
    are merged and deduplicated while preserving chunk diversity across hops.

    Args:
        original_query: The user's original query before any augmentation.
            Passed as semantic_query so the reranker scores against intent
            rather than the hop-augmented search text.
    """
    all_chunks: list[dict] = []
    seen_ids: set[str] = set()

    # Reranker should score against the user's original intent, not the
    # hop-augmented search_text (e.g. "query raw to int").
    reranker_query = original_query or base_query

    for src, tgt in hops:
        hop_query = f"{base_query} {src} to {tgt}"
        try:
            results = await search_client.search(hop_query, top_k=top_k_per_hop, semantic_query=reranker_query)
            new_count = 0
            for doc in results:
                doc_id = doc.get("content", "")[:200]
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_chunks.append(doc)
                    new_count += 1
            logger.info(
                "STTM hop %s→%s: %d retrieved, %d new",
                src, tgt, len(results), new_count,
            )
        except Exception as e:
            logger.warning("STTM hop %s→%s failed: %s", src, tgt, e)

    all_chunks.sort(
        key=lambda d: (d.get("reranker_score") or 0, d.get("score", 0)),
        reverse=True,
    )

    logger.info("STTM hop search: %d chunks from %d hops", len(all_chunks), len(hops))
    return all_chunks
