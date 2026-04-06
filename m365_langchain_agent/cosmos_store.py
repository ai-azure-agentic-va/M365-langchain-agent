"""CosmosDB conversation history store.

Stores per-conversation message history so the agent can maintain
multi-turn context. Each conversation is keyed by the Bot Framework
conversation ID.

Database:  configured via AZURE_COSMOS_DATABASE env var
Container: configured via AZURE_COSMOS_CONTAINER env var (partition key: /conversation_id)
TTL:       24 hours (configurable via COSMOS_TTL_SECONDS)
"""

import logging
import os
import time
from typing import List, Dict, Optional

from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos.exceptions import CosmosResourceNotFoundError

logger = logging.getLogger(__name__)

_store = None


def get_cosmos_store():
    """Singleton — reuses the same store across invocations."""
    global _store
    if _store is None:
        _store = CosmosConversationStore()
    return _store


class CosmosConversationStore:
    """Manages conversation history in CosmosDB."""

    def __init__(self):
        endpoint = os.environ["AZURE_COSMOS_ENDPOINT"]
        db_name = os.environ.get("AZURE_COSMOS_DATABASE", "m365-langchain-agent")
        container_name = os.environ.get("AZURE_COSMOS_CONTAINER", "conversations")
        self.ttl_seconds = int(os.environ.get("COSMOS_TTL_SECONDS", "86400"))  # 24h

        credential = DefaultAzureCredential()
        self.client = CosmosClient(endpoint, credential=credential)
        self.database = self.client.create_database_if_not_exists(id=db_name)
        self.container = self.database.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path="/conversation_id"),
            default_ttl=self.ttl_seconds,
        )
        logger.info(
            f"[CosmosStore] Initialized: db={db_name}, container={container_name}, "
            f"ttl={self.ttl_seconds}s"
        )

    def get_history(self, conversation_id: str, user_id: Optional[str] = None) -> List[Dict]:
        """Get conversation history for a given conversation.

        Args:
            conversation_id: The conversation ID
            user_id: Optional user ID for validation (ensures user owns this conversation)

        Returns:
            List of {"role": "user"|"assistant", "content": "..."} dicts,
            ordered chronologically.
        """
        try:
            item = self.container.read_item(
                item=conversation_id,
                partition_key=conversation_id,
            )
            # If user_id is provided, validate ownership
            if user_id and item.get("user_id") != user_id:
                logger.warning(
                    f"[CosmosStore] User {user_id} attempted to access conversation {conversation_id} "
                    f"owned by {item.get('user_id', 'unknown')}"
                )
                return []
            return item.get("messages", [])
        except CosmosResourceNotFoundError:
            return []
        except Exception as e:
            logger.error(f"[CosmosStore] Failed to read history: {e}")
            return []

    def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        bot_response: str,
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        user_display_name: Optional[str] = None,
    ) -> None:
        """Append a user/bot turn to the conversation history.

        Creates the document if it doesn't exist, or updates it with
        the new turn appended. Stamps user identity on the conversation.

        Args:
            conversation_id: The conversation ID
            user_message: The user's message
            bot_response: The bot's response
            user_id: Entra ID Object ID (oid) of the user
            user_email: User's email address
            user_display_name: User's display name
        """
        try:
            # Try to read existing conversation
            try:
                item = self.container.read_item(
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

            # Stamp user identity on first turn (or update if changed)
            if user_id:
                item["user_id"] = user_id
            if user_email:
                item["user_email"] = user_email
            if user_display_name:
                item["user_display_name"] = user_display_name

            # Append the new turn
            messages.append({"role": "user", "content": user_message})
            messages.append({"role": "assistant", "content": bot_response})

            # Keep only the last 20 messages (10 turns) to avoid unbounded growth
            max_messages = int(os.environ.get("COSMOS_MAX_MESSAGES", "20"))
            if len(messages) > max_messages:
                messages = messages[-max_messages:]

            item["messages"] = messages
            item["updated_at"] = time.time()

            self.container.upsert_item(item)
            logger.info(
                f"[CosmosStore] Saved turn for conversation={conversation_id}, "
                f"user={user_id or 'anonymous'}, total_messages={len(messages)}"
            )
        except Exception as e:
            logger.error(f"[CosmosStore] Failed to save turn: {e}")
