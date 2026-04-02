"""Chainlit data layer backed by CosmosDB.

Enables the conversation history sidebar in Chainlit UI.
All users can see all threads (no auth filtering).
Reads/writes to the same CosmosDB container used by cosmos_store.py.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from chainlit.data import BaseDataLayer
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    Pagination,
    PaginatedResponse,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

from m365_langchain_agent.cosmos_store import get_cosmos_store

logger = logging.getLogger(__name__)


def _ts_to_iso(ts) -> str:
    """Convert a Unix timestamp to ISO 8601 string for Chainlit UI."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=timezone.utc).isoformat()


class CosmosDataLayer(BaseDataLayer):
    """Minimal data layer that exposes CosmosDB conversations in Chainlit's sidebar."""

    def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    # -- User management (no auth, return a default user) --

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        return PersistedUser(id=identifier, identifier=identifier, createdAt=_ts_to_iso(time.time()))

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return PersistedUser(id=user.identifier, identifier=user.identifier, createdAt=_ts_to_iso(time.time()))

    # -- Thread listing (sidebar) --

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        """List conversation threads for the current user in the sidebar."""
        try:
            cosmos = get_cosmos_store()

            # Filter by user_id if provided in filters.userId
            # If no userId filter, show all threads (backward compatibility for legacy data)
            if filters.userId and filters.userId != "default-user":
                query = "SELECT * FROM c WHERE c.user_id = @user_id ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                params = [
                    {"name": "@user_id", "value": filters.userId},
                    {"name": "@limit", "value": pagination.first or 20}
                ]

                if pagination.cursor:
                    query = "SELECT * FROM c WHERE c.user_id = @user_id AND c.updated_at < @cursor ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                    params.append({"name": "@cursor", "value": float(pagination.cursor)})
            else:
                # Legacy: show all threads (for backward compatibility)
                query = "SELECT * FROM c ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                params = [{"name": "@limit", "value": pagination.first or 20}]

                if pagination.cursor:
                    query = "SELECT * FROM c WHERE c.updated_at < @cursor ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                    params.append({"name": "@cursor", "value": float(pagination.cursor)})

            items = list(cosmos.container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            ))

            threads = []
            for item in items:
                messages = item.get("messages", [])
                if not messages:
                    continue

                # First user message becomes the thread name
                first_user_msg = next(
                    (m["content"][:80] for m in messages if m["role"] == "user"), "New chat"
                )

                # Convert messages to Chainlit StepDicts
                steps = []
                for i, msg in enumerate(messages):
                    step_type = "user_message" if msg["role"] == "user" else "assistant_message"
                    created = item.get("created_at", time.time())
                    steps.append(StepDict(
                        id=f"{item['id']}-{i}",
                        threadId=item["id"],
                        name=msg["role"],
                        type=step_type,
                        output=msg["content"],
                        input="",
                        streaming=False,
                        metadata={},
                        createdAt=_ts_to_iso(created + i),
                    ))

                # Use the actual user_id from the conversation, or fall back to "default-user"
                user_id = item.get("user_id", "default-user")

                threads.append(ThreadDict(
                    id=item["id"],
                    createdAt=_ts_to_iso(item.get("created_at", time.time())),
                    name=first_user_msg,
                    userId=user_id,
                    userIdentifier=user_id,
                    tags=None,
                    metadata=None,
                    steps=steps,
                    elements=None,
                ))

            logger.info(f"[DataLayer] Listed {len(threads)} threads")
            return PaginatedResponse(
                data=threads,
                pageInfo={
                    "hasNextPage": len(threads) >= (pagination.first or 20),
                    "startCursor": str(items[0].get("updated_at", "")) if items else None,
                    "endCursor": str(items[-1].get("updated_at", "")) if items else None,
                },
            )
        except Exception as e:
            logger.error(f"[DataLayer] list_threads failed: {e}")
            return PaginatedResponse(data=[], pageInfo={"hasNextPage": False, "startCursor": None, "endCursor": None})

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        """Load a specific thread's messages."""
        try:
            cosmos = get_cosmos_store()
            item = cosmos.container.read_item(item=thread_id, partition_key=thread_id)
            messages = item.get("messages", [])

            steps = []
            created = item.get("created_at", time.time())
            for i, msg in enumerate(messages):
                step_type = "user_message" if msg["role"] == "user" else "assistant_message"
                steps.append(StepDict(
                    id=f"{thread_id}-{i}",
                    threadId=thread_id,
                    name=msg["role"],
                    type=step_type,
                    output=msg["content"],
                    input="",
                    streaming=False,
                    metadata={},
                    createdAt=_ts_to_iso(created + i),
                ))

            # Use the actual user_id from the conversation, or fall back to "default-user"
            user_id = item.get("user_id", "default-user")

            return ThreadDict(
                id=thread_id,
                createdAt=_ts_to_iso(item.get("created_at", time.time())),
                name=next((m["content"][:80] for m in messages if m["role"] == "user"), "Chat"),
                userId=user_id,
                userIdentifier=user_id,
                tags=None,
                metadata=None,
                steps=steps,
                elements=None,
            )
        except Exception as e:
            logger.error(f"[DataLayer] get_thread failed for {thread_id}: {e}")
            return None

    async def get_thread_author(self, thread_id: str) -> str:
        """Return the actual user_id who owns this thread."""
        try:
            cosmos = get_cosmos_store()
            item = cosmos.container.read_item(item=thread_id, partition_key=thread_id)
            return item.get("user_id", "default-user")
        except Exception as e:
            logger.error(f"[DataLayer] get_thread_author failed for {thread_id}: {e}")
            return "default-user"

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        pass

    async def delete_thread(self, thread_id: str) -> None:
        try:
            cosmos = get_cosmos_store()
            cosmos.container.delete_item(item=thread_id, partition_key=thread_id)
            logger.info(f"[DataLayer] Deleted thread {thread_id}")
        except Exception as e:
            logger.error(f"[DataLayer] delete_thread failed: {e}")

    # -- Steps (no-op, we save via cosmos_store.save_turn) --

    async def create_step(self, step_dict: StepDict) -> None:
        pass

    async def update_step(self, step_dict: StepDict) -> None:
        pass

    async def delete_step(self, step_id: str) -> None:
        pass

    # -- Elements (not used) --

    async def create_element(self, element) -> None:
        pass

    async def get_element(self, thread_id: str, element_id: str):
        return None

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None) -> None:
        pass

    # -- Feedback (stub) --

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    # -- Favorites (stub) --

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        return []

    async def set_step_favorite(self, step_dict: StepDict, favorite: bool) -> StepDict:
        return step_dict
