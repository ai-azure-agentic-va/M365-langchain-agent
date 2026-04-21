"""Bot Framework ActivityHandler — bridges Bot Service and the RAG agent."""

import logging

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes

from m365_langchain_agent.config import settings
from m365_langchain_agent.core.agent import invoke_agent, format_sources_markdown
from m365_langchain_agent.cosmos import get_cosmos_store

logger = logging.getLogger(__name__)


class DocAgentBot(ActivityHandler):

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        user_text = turn_context.activity.text
        if not user_text or not user_text.strip():
            await turn_context.send_activity("I didn't receive a message. Please try again.")
            return

        conversation_id = turn_context.activity.conversation.id

        user_id = None
        user_name = None
        user_email = None

        if turn_context.activity.from_property:
            user_id = getattr(turn_context.activity.from_property, "aad_object_id", None)
            user_name = getattr(turn_context.activity.from_property, "name", None)

        logger.info(
            "Message: conversation=%s, user=%s, text=%s",
            conversation_id, user_id or "anonymous", user_text[:100],
        )

        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        try:
            cosmos = await get_cosmos_store()
            history = await cosmos.get_history(conversation_id, user_id=user_id)
        except Exception as e:
            logger.error("CosmosDB read failed: %s", e)
            history = []

        try:
            result = await invoke_agent(query=user_text, conversation_history=history)
        except Exception as e:
            logger.error("Agent invocation failed: %s", e)
            await turn_context.send_activity(
                "I'm sorry, I encountered an error processing your request. Please try again."
            )
            return
        answer = result["answer"]
        sources = result["sources"]

        sources_md = format_sources_markdown(sources)
        full_response = f"{answer}\n\n---\n**Sources:**\n{sources_md}" if sources_md else answer

        try:
            cosmos = await get_cosmos_store()
            await cosmos.save_turn(
                conversation_id=conversation_id,
                user_message=user_text,
                bot_response=answer,
                user_id=user_id,
                user_email=user_email,
                user_display_name=user_name,
            )
        except Exception as e:
            logger.error("CosmosDB write failed: %s", e)

        await turn_context.send_activity(full_response)
        logger.info("Response sent: conversation=%s, sources=%d", conversation_id, len(sources))

    async def on_members_added_activity(self, members_added, turn_context: TurnContext) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                welcome = (
                    f"Hello! I'm the **{settings.app_display_name}**. Ask me questions about "
                    "internal policies, procedures, and documentation. "
                    "I'll search the knowledge base and provide answers with "
                    "**clickable source links** and citations."
                )
                await turn_context.send_activity(welcome)
                logger.info("Welcome sent to member=%s", member.id)
