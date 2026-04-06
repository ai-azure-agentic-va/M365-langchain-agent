"""Azure AI Search client — hybrid search (keyword + vector + semantic).

Queries Azure AI Search index populated by the ingestion pipeline.

Index schema:
    id              Edm.String      (key)
    chunk_content   Edm.String      searchable, en.microsoft analyzer
    content_vector  Collection(Edm.Single)  3072d, HNSW cosine, retrievable
    document_title  Edm.String      searchable, filterable
    source_url      Edm.String      filterable
    source_type     Edm.String      filterable, facetable ("sharepoint" | "wiki")
    file_name       Edm.String      filterable
    chunk_index     Edm.Int32       filterable
    total_chunks    Edm.Int32
    page_number     Edm.Int32       filterable
    last_modified   Edm.DateTimeOffset
    ingested_at     Edm.DateTimeOffset
    pii_redacted    Edm.Boolean     filterable
"""

import logging
import os
from typing import List, Dict

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from langchain_openai import AzureOpenAIEmbeddings

logger = logging.getLogger(__name__)

_client = None


def get_search_client():
    """Singleton — reuses the same client across invocations."""
    global _client
    if _client is None:
        _client = AzureSearchClient()
    return _client


class AzureSearchClient:
    """Thin wrapper around azure-search-documents for hybrid search."""

    def __init__(self):
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

        self.search_client = SearchClient(
            endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            index_name=os.environ["AZURE_SEARCH_INDEX_NAME"],
            credential=credential,
        )
        self.embeddings = AzureOpenAIEmbeddings(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token_provider,
            azure_deployment=os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-05-01-preview"),
            dimensions=int(os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
        )
        self.semantic_config = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG_NAME", "")
        self.vector_field = os.environ.get("AZURE_SEARCH_EMBEDDING_FIELD", "content_vector")

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_expr: str = None,
        vector_search_mode: str = "hnsw",
        search_strategy: str = "hybrid",
    ) -> List[Dict]:
        """Search with configurable vector mode and strategy.

        Args:
            query: The search query text.
            top_k: Number of results to return.
            filter_expr: Optional OData filter expression for metadata filtering.
            vector_search_mode: "hnsw" (approximate, fast) or "exhaustive_knn" (exact, slower).
            search_strategy: "hybrid" (keyword+vector), "vector" (vector only), or "keyword" (keyword only).
        """
        if not query or not query.strip():
            return []

        # Over-retrieve candidates so the semantic reranker has a wider pool,
        # then trim to top_k after reranking. This surfaces relevant documents
        # that would otherwise be invisible at position > top_k.
        retrieval_k = max(top_k * 3, 15) if self.semantic_config else top_k

        use_vector = search_strategy in ("hybrid", "vector")
        use_keyword = search_strategy in ("hybrid", "keyword")

        search_kwargs = dict(
            top=retrieval_k,
            query_type="semantic" if self.semantic_config and use_keyword else "simple",
            semantic_configuration_name=(self.semantic_config or None) if use_keyword else None,
        )

        if use_keyword:
            search_kwargs["search_text"] = query
        else:
            search_kwargs["search_text"] = "*"

        if use_vector:
            query_vector = self.embeddings.embed_query(query)
            vector_query = VectorizedQuery(
                vector=query_vector,
                k=retrieval_k,
                fields=self.vector_field,
                exhaustive=(vector_search_mode == "exhaustive_knn"),
            )
            search_kwargs["vector_queries"] = [vector_query]

        if filter_expr:
            search_kwargs["filter"] = filter_expr
            logger.info(f"[Search] Applying filter: {filter_expr}")

        results = self.search_client.search(**search_kwargs)

        docs = []
        for r in results:
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
            })

        docs = docs[:top_k]

        logger.info(
            f"[Search] query='{query[:80]}...' hits={len(docs)} (retrieved={retrieval_k}, returned={top_k}) "
            f"vector_mode={vector_search_mode}, strategy={search_strategy}, "
            f"index={os.environ.get('AZURE_SEARCH_INDEX_NAME')}"
        )
        return docs

    def search_document_names(self, query: str, top: int = 50) -> list[str]:
        """Lightweight query to discover ALL matching document names via faceting.

        Uses ``select`` to retrieve only metadata (no chunk content) and
        ``facets`` on ``file_name`` to aggregate unique document names.
        Near-zero token cost — no embeddings or content transferred.

        Returns:
            Deduplicated list of file_name values matching the query.
        """
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
            results = self.search_client.search(**search_kwargs)
            facets = results.get_facets()
            if facets and "file_name" in facets:
                names = [f.value for f in facets["file_name"] if f.value]
                logger.info(f"[Search] Document discovery: {len(names)} unique docs via facets")
                return names

            # Fallback: deduplicate from result rows
            seen: set[str] = set()
            names: list[str] = []
            for r in results:
                name = r.get("file_name") or r.get("document_title")
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            logger.info(f"[Search] Document discovery: {len(names)} unique docs (fallback)")
            return names
        except Exception as e:
            logger.warning(f"[Search] Document discovery failed: {e}")
            return []
