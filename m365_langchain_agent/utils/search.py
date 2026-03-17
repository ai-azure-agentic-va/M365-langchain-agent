"""Azure AI Search client — hybrid search (keyword + vector + semantic).

Queries Azure AI Search index populated by the ingestion pipeline.

Index schema:
    id              Edm.String      (key)
    chunk_content   Edm.String      searchable, en.microsoft analyzer
    content_vector  Collection(Edm.Single)  1536d, HNSW cosine, retrievable
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
        )
        self.semantic_config = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG_NAME", "")
        self.vector_field = os.environ.get("AZURE_SEARCH_EMBEDDING_FIELD", "content_vector")

    def search(self, query: str, top_k: int = 5, filter_expr: str = None) -> List[Dict]:
        """Hybrid search: keyword + vector + semantic ranking.

        Args:
            query: The search query text.
            top_k: Number of results to return.
            filter_expr: Optional OData filter expression for metadata filtering.
                Examples:
                    "file_name eq 'policy.pdf'"
                    "source_type eq 'wiki'"
                    "document_title eq 'Refund Policy'"
                    "pii_redacted eq true"
        """
        if not query or not query.strip():
            return []

        query_vector = self.embeddings.embed_query(query)

        vector_query = VectorizedQuery(
            vector=query_vector,
            k=top_k,
            fields=self.vector_field,
        )

        search_kwargs = dict(
            search_text=query,
            vector_queries=[vector_query],
            top=top_k,
            query_type="semantic" if self.semantic_config else "simple",
            semantic_configuration_name=self.semantic_config or None,
        )
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

        logger.info(
            f"[Search] query='{query[:80]}...' hits={len(docs)} "
            f"index={os.environ.get('AZURE_SEARCH_INDEX_NAME')}"
        )
        return docs
