"""Async Azure AI Search client — hybrid search (keyword + vector + semantic)."""

import asyncio
import logging

from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery
from langchain_openai import AzureOpenAIEmbeddings

from m365_langchain_agent.config import settings, credential, token_provider

logger = logging.getLogger(__name__)

_client: "AsyncSearchClient | None" = None
_lock = asyncio.Lock()


async def get_search_client() -> "AsyncSearchClient":
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is None:
            _client = AsyncSearchClient()
    return _client


async def close_search_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


class AsyncSearchClient:

    def __init__(self) -> None:
        self._search_client = SearchClient(
            endpoint=settings.azure_search_endpoint,
            index_name=settings.azure_search_index_name,
            credential=credential,
        )
        self.embeddings = AzureOpenAIEmbeddings(
            azure_endpoint=settings.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            azure_deployment=settings.azure_openai_embedding_deployment,
            api_version=settings.azure_openai_api_version,
            dimensions=settings.azure_openai_embedding_dimensions,
        )
        self.semantic_config = settings.azure_search_semantic_config_name
        self.vector_field = settings.azure_search_embedding_field
        self.exhaustive_knn = settings.search_exhaustive_knn

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter_expr: str | None = None,
        semantic_query: str | None = None,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        retrieval_k = max(top_k * 3, 15) if self.semantic_config else top_k

        try:
            query_vector = await self.embeddings.aembed_query(query)
        except Exception as e:
            logger.error("Embedding failed for query '%s': %s", query[:80], e)
            return []

        vector_query = VectorizedQuery(
            vector=query_vector,
            k_nearest_neighbors=retrieval_k,
            fields=self.vector_field,
            exhaustive=True if self.exhaustive_knn else None,
        )

        search_kwargs = dict(
            search_text=query,
            vector_queries=[vector_query],
            top=retrieval_k,
            select=[
                "chunk_content", "document_title", "source_url", "source_type",
                "file_name", "chunk_index", "total_chunks", "page_number",
                "pii_redacted", "breadcrumb",
            ],
            include_total_count=True,
            query_type="semantic" if self.semantic_config else "simple",
            semantic_configuration_name=self.semantic_config or None,
        )
        # Pass original user intent so reranker scores against intent, not augmented text
        if self.semantic_config and semantic_query:
            search_kwargs["semantic_query"] = semantic_query
        if filter_expr:
            search_kwargs["filter"] = filter_expr
            logger.info("Search filter: %s", filter_expr)

        try:
            results = await self._search_client.search(**search_kwargs)

            docs = []
            async for r in results:
                docs.append({
                    "content": r.get("chunk_content", ""),
                    "score": r.get("@search.score", 0.0),
                    "reranker_score": r.get("@search.reranker_score", None),
                    "document_title": r.get("document_title", ""),
                    "source_url": r.get("source_url", ""),
                    "source_type": r.get("source_type", ""),
                    "file_name": r.get("file_name", ""),
                    "chunk_index": r.get("chunk_index", 0),
                    "total_chunks": r.get("total_chunks", 0),
                    "page_number": r.get("page_number", 0),
                    "pii_redacted": r.get("pii_redacted", False),
                    "breadcrumb": r.get("breadcrumb", ""),
                })

            total_count = getattr(results, "get_count", lambda: None)()
        except Exception as e:
            logger.error("Search failed for query '%s': %s", query[:80], e)
            return []

        docs.sort(
            key=lambda d: (d.get("reranker_score") or 0, d.get("score", 0)),
            reverse=True,
        )
        docs = docs[:top_k]

        total_label = f", total={total_count}" if total_count is not None else ""
        logger.info(
            "Search: query='%s' hits=%d (retrieved=%d, returned=%d%s)",
            query[:80], len(docs), retrieval_k, top_k, total_label,
        )
        return docs

    async def search_document_names(self, query: str, top: int = 50) -> list[str]:
        if not query or not query.strip():
            return []

        search_kwargs: dict = dict(
            search_text=query,
            top=top,
            select=["file_name", "document_title"],
            facets=["file_name,count:50"],
            query_type="semantic" if self.semantic_config else "simple",
            semantic_configuration_name=self.semantic_config or None,
        )
        if self.semantic_config:
            search_kwargs["semantic_query"] = query

        try:
            results = await self._search_client.search(**search_kwargs)
            facets = await results.get_facets()
            if facets and "file_name" in facets:
                names = [f.value for f in facets["file_name"] if f.value]
                logger.info("Document discovery: %d unique docs via facets", len(names))
                return names

            seen: set[str] = set()
            names: list[str] = []
            async for r in results:
                name = r.get("file_name") or r.get("document_title")
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            logger.info("Document discovery: %d unique docs (fallback)", len(names))
            return names
        except Exception as e:
            logger.warning("Document discovery failed: %s", e)
            return []

    async def close(self) -> None:
        await self._search_client.close()
        logger.info("Search client closed")
