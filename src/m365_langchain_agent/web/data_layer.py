"""Chainlit data layer backed by CosmosDB."""

import logging
import time
import uuid
from datetime import datetime, timezone

from azure.cosmos.exceptions import CosmosResourceNotFoundError

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

from m365_langchain_agent.cosmos import get_cosmos_store

logger = logging.getLogger(__name__)


def _ts_to_iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=timezone.utc).isoformat()


class CosmosDataLayer(BaseDataLayer):

    def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    async def get_user(self, identifier: str) -> PersistedUser | None:
        return PersistedUser(id=identifier, identifier=identifier, createdAt=_ts_to_iso(time.time()))

    async def create_user(self, user: User) -> PersistedUser | None:
        return PersistedUser(id=user.identifier, identifier=user.identifier, createdAt=_ts_to_iso(time.time()))

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        logger.info(
            "list_threads called: userId=%s cursor=%s first=%s",
            filters.userId, pagination.cursor, pagination.first,
        )
        try:
            cosmos = await get_cosmos_store()

            if filters.userId and filters.userId != "default-user":
                query = "SELECT * FROM c WHERE c.user_id = @user_id ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                params = [
                    {"name": "@user_id", "value": filters.userId},
                    {"name": "@limit", "value": pagination.first or 20},
                ]

                if pagination.cursor:
                    query = "SELECT * FROM c WHERE c.user_id = @user_id AND c.updated_at < @cursor ORDER BY c.updated_at DESC OFFSET 0 LIMIT @limit"
                    params.append({"name": "@cursor", "value": float(pagination.cursor)})
            else:
                logger.info("list_threads: no userId filter, returning empty")
                return PaginatedResponse(
                    data=[],
                    pageInfo={"hasNextPage": False, "startCursor": None, "endCursor": None},
                )

            items = []
            async for item in cosmos.container.query_items(
                query=query,
                parameters=params,
            ):
                items.append(item)

            threads = []
            for item in items:
                messages = item.get("messages", [])
                stub_name = item.get("name")
                # Allow stub threads (no messages yet) if they have a name from update_thread
                if not messages and not stub_name:
                    continue

                first_user_msg = stub_name or next(
                    (m["content"][:80] for m in messages if m["role"] == "user"), "New chat"
                )

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

            logger.info("Listed %d threads", len(threads))
            return PaginatedResponse(
                data=threads,
                pageInfo={
                    "hasNextPage": len(threads) >= (pagination.first or 20),
                    "startCursor": str(items[0].get("updated_at", "")) if items else None,
                    "endCursor": str(items[-1].get("updated_at", "")) if items else None,
                },
            )
        except Exception as e:
            logger.error("list_threads failed: %s", e)
            return PaginatedResponse(data=[], pageInfo={"hasNextPage": False, "startCursor": None, "endCursor": None})

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        try:
            cosmos = await get_cosmos_store()
            item = await cosmos.container.read_item(item=thread_id, partition_key=thread_id)
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
            logger.error("get_thread failed for %s: %s", thread_id, e)
            return None

    async def get_thread_author(self, thread_id: str) -> str:
        try:
            cosmos = await get_cosmos_store()
            item = await cosmos.container.read_item(item=thread_id, partition_key=thread_id)
            return item.get("user_id", "default-user")
        except Exception as e:
            logger.error("get_thread_author failed for %s: %s", thread_id, e)
            return "default-user"

    async def update_thread(self, thread_id: str, name=None, user_id=None, metadata=None, tags=None) -> None:
        # Chainlit calls this BEFORE emitting first_interaction (see chainlit.emitter.flush_thread_queues).
        # We must persist a stub doc here so the subsequent /project/threads refetch sees the new thread.
        #
        # Important invariants:
        #   - Never overwrite an existing user_id with "default-user" (would orphan SSO-owned threads).
        #   - Never overwrite the messages array — that field is owned by save_turn. We always read the
        #     existing doc first and preserve its messages on upsert. If the doc didn't exist when we
        #     read it but does by the time we write (race with save_turn), we re-read and merge.
        safe_user_id = user_id if user_id and user_id != "default-user" else None
        safe_name = name[:200] if name else None

        try:
            cosmos = await get_cosmos_store()

            async def _apply(existing: dict, created_flag: bool) -> None:
                if safe_user_id:
                    existing["user_id"] = safe_user_id
                if safe_name:
                    existing["name"] = safe_name
                if metadata is not None:
                    existing["metadata"] = metadata
                if tags is not None:
                    existing["tags"] = tags
                existing["updated_at"] = time.time()
                await cosmos.container.upsert_item(existing)
                logger.info(
                    "update_thread: thread_id=%s user_id=%s name=%s created=%s",
                    thread_id, safe_user_id, (safe_name or ""), created_flag,
                )

            try:
                item = await cosmos.container.read_item(
                    item=thread_id, partition_key=thread_id,
                )
                await _apply(item, False)
                return
            except CosmosResourceNotFoundError:
                pass

            # Doc didn't exist: create a stub. If save_turn raced ahead and already created
            # the doc, re-read and merge to avoid clobbering its messages.
            stub = {
                "id": thread_id,
                "conversation_id": thread_id,
                "messages": [],
                "created_at": time.time(),
            }
            await _apply(stub, True)
        except Exception as e:
            logger.error("update_thread failed for %s: %s — attempting race recovery", thread_id, e)
            try:
                cosmos = await get_cosmos_store()
                existing = await cosmos.container.read_item(
                    item=thread_id, partition_key=thread_id,
                )
                if safe_user_id:
                    existing["user_id"] = safe_user_id
                if safe_name:
                    existing["name"] = safe_name
                existing["updated_at"] = time.time()
                await cosmos.container.upsert_item(existing)
                logger.info("update_thread: race recovered thread_id=%s", thread_id)
            except Exception as race_err:
                logger.error("update_thread race recovery failed for %s: %s", thread_id, race_err)

    async def delete_thread(self, thread_id: str) -> None:
        try:
            cosmos = await get_cosmos_store()
            await cosmos.container.delete_item(item=thread_id, partition_key=thread_id)
            logger.info("Deleted thread %s", thread_id)
        except Exception as e:
            logger.error("delete_thread failed: %s", e)

    async def create_step(self, step_dict: StepDict) -> None:
        pass

    async def update_step(self, step_dict: StepDict) -> None:
        pass

    async def delete_step(self, step_id: str) -> None:
        pass

    async def create_element(self, element) -> None:
        pass

    async def get_element(self, thread_id: str, element_id: str):
        return None

    async def delete_element(self, element_id: str, thread_id=None) -> None:
        pass

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []

    async def set_step_favorite(self, step_dict: StepDict, favorite: bool) -> StepDict:
        return step_dict
