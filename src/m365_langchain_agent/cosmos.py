"""Async CosmosDB conversation history store.

Partition key: /conversation_id. TTL: configurable (default 24h).
"""

import asyncio
import logging
import time

from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from m365_langchain_agent.config import settings, credential
from m365_langchain_agent.exceptions import CosmosError

logger = logging.getLogger(__name__)

_store: "AsyncCosmosStore | None" = None
_cosmos_lock = asyncio.Lock()


async def get_cosmos_store() -> "AsyncCosmosStore":
    global _store
    if _store is not None:
        return _store
    async with _cosmos_lock:
        if _store is None:
            _store = AsyncCosmosStore()
            await _store.initialize()
    return _store


async def close_cosmos_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None


class AsyncCosmosStore:

    def __init__(self) -> None:
        self.client = CosmosClient(
            settings.azure_cosmos_endpoint, credential=credential
        )
        self.database = None
        self.container = None

    async def initialize(self) -> None:
        self.database = await self.client.create_database_if_not_exists(
            id=settings.azure_cosmos_database
        )
        self.container = await self.database.create_container_if_not_exists(
            id=settings.azure_cosmos_container,
            partition_key=PartitionKey(path="/conversation_id"),
            default_ttl=settings.cosmos_ttl_seconds,
        )
        logger.info(
            "CosmosStore initialized: db=%s, container=%s, ttl=%ds",
            settings.azure_cosmos_database,
            settings.azure_cosmos_container,
            settings.cosmos_ttl_seconds,
        )

    async def get_history(self, conversation_id: str, user_id: str | None = None) -> list[dict]:
        try:
            item = await self.container.read_item(
                item=conversation_id,
                partition_key=conversation_id,
            )
            if user_id and item.get("user_id") != user_id:
                logger.warning(
                    "User %s attempted to access conversation %s owned by %s",
                    user_id, conversation_id, item.get("user_id", "unknown"),
                )
                return []
            return item.get("messages", [])
        except CosmosResourceNotFoundError:
            return []
        except Exception as e:
            raise CosmosError(f"Failed to read history: {e}") from e

    async def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        bot_response: str,
        user_id: str | None = None,
        user_email: str | None = None,
        user_display_name: str | None = None,
    ) -> None:
        try:
            try:
                item = await self.container.read_item(
                    item=conversation_id,
                    partition_key=conversation_id,
                )
                messages = item.get("messages", [])
            except CosmosResourceNotFoundError:
                item = {
                    "id": conversation_id,
                    "conversation_id": conversation_id,
                    "messages": [],
                    "created_at": time.time(),
                }
                messages = []

            if user_id:
                item["user_id"] = user_id
            if user_email:
                item["user_email"] = user_email
            if user_display_name:
                item["user_display_name"] = user_display_name

            messages.append({"role": "user", "content": user_message})
            messages.append({"role": "assistant", "content": bot_response})

            if len(messages) > settings.cosmos_max_messages:
                messages = messages[-settings.cosmos_max_messages:]

            item["messages"] = messages
            item["updated_at"] = time.time()

            await self.container.upsert_item(item)
            logger.info(
                "Saved turn: conversation=%s, user=%s, messages=%d",
                conversation_id, user_id or "anonymous", len(messages),
            )
        except CosmosResourceNotFoundError:
            raise
        except Exception as e:
            raise CosmosError(f"Failed to save turn: {e}") from e

    async def close(self) -> None:
        await self.client.close()
        logger.info("CosmosStore closed")
