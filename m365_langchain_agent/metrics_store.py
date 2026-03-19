"""CosmosDB metrics store for tracking token usage, groundedness scores, and content safety evaluations.

Stores per-query metrics in a separate CosmosDB container for analytics.
Does NOT display metrics to the end user — backend/logging only.

Container: configured via AZURE_COSMOS_METRICS_CONTAINER env var (default: "metrics")
Partition key: /conversation_id
"""

import logging
import os
import time
import uuid
from typing import Dict, Optional

from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient

logger = logging.getLogger(__name__)

_store = None


def get_metrics_store():
    """Singleton — reuses the same store across invocations."""
    global _store
    if _store is None:
        _store = MetricsStore()
    return _store


class MetricsStore:
    """Manages query metrics in CosmosDB."""

    def __init__(self):
        endpoint = os.environ["AZURE_COSMOS_ENDPOINT"]
        db_name = os.environ.get("AZURE_COSMOS_DATABASE", "m365-langchain-agent")
        container_name = os.environ.get("AZURE_COSMOS_METRICS_CONTAINER", "metrics")

        credential = DefaultAzureCredential()
        self.client = CosmosClient(endpoint, credential=credential)
        self.database = self.client.get_database_client(db_name)
        self.container = self.database.get_container_client(container_name)
        logger.info(f"[MetricsStore] Initialized: db={db_name}, container={container_name}")

    def save_metrics(
        self,
        conversation_id: str,
        query: str,
        model: str,
        token_usage: Dict,
        content_safety: Optional[Dict] = None,
    ) -> None:
        """Save query metrics to CosmosDB.

        Args:
            conversation_id: The conversation thread ID.
            query: The user's question (truncated for storage).
            model: The model deployment name used.
            token_usage: {"input_tokens": N, "output_tokens": N, "total_tokens": N}
            content_safety: Optional evaluation results (groundedness, safety scores).
        """
        try:
            item = {
                "id": str(uuid.uuid4()),
                "conversation_id": conversation_id,
                "timestamp": time.time(),
                "query": query[:200],
                "model": model,
                "input_tokens": token_usage.get("input_tokens", 0),
                "output_tokens": token_usage.get("output_tokens", 0),
                "total_tokens": token_usage.get("total_tokens", 0),
            }

            if content_safety:
                item["groundedness_score"] = content_safety.get("groundedness_score")
                item["groundedness_reason"] = content_safety.get("groundedness_reason", "")
                item["violence"] = content_safety.get("violence", "")
                item["sexual"] = content_safety.get("sexual", "")
                item["self_harm"] = content_safety.get("self_harm", "")
                item["hate_unfairness"] = content_safety.get("hate_unfairness", "")

            self.container.upsert_item(item)
            logger.info(
                f"[MetricsStore] Saved metrics: conversation={conversation_id}, "
                f"model={model}, tokens={token_usage.get('total_tokens', 0)}"
            )
        except Exception as e:
            logger.error(f"[MetricsStore] Failed to save metrics: {e}")
