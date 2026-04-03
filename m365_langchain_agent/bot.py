"""Bot Framework ActivityHandler — bridges Bot Service <> LangChain agent.

Receives Bot Framework Activity messages from Azure Bot Service,
invokes the LangChain RAG agent, stores conversation history in CosmosDB,
and sends the response back through Bot Service with clickable citation links.
"""

import logging

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes

from m365_langchain_agent.agent import invoke_agent, format_sources_markdown
from m365_langchain_agent.cosmos_store import get_cosmos_store

logger = logging.getLogger(__name__)


class DocAgentBot(ActivityHandler):
    """Bot that handles incoming messages and routes them to the RAG agent."""

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Handle incoming user messages."""
        user_text = turn_context.activity.text
        if not user_text or not user_text.strip():
            await turn_context.send_activity("I didn't receive a message. Please try again.")
            return

        conversation_id = turn_context.activity.conversation.id
        logger.info(
            f"[Bot] Message received: conversation={conversation_id}, "
            f"text={user_text[:100]}"
        )

        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        try:
            cosmos = get_cosmos_store()
            history = cosmos.get_history(conversation_id)
        except Exception as e:
            logger.error(f"[Bot] CosmosDB read failed: {e}")
            history = []

        result = await invoke_agent(query=user_text, conversation_history=history)
        answer = result["answer"]
        sources = result["sources"]

        sources_md = format_sources_markdown(sources)
        full_response = f"{answer}\n\n---\n**Sources:**\n{sources_md}" if sources_md else answer

        try:
            cosmos = get_cosmos_store()
            cosmos.save_turn(
                conversation_id=conversation_id,
                user_message=user_text,
                bot_response=answer,
            )
        except Exception as e:
            logger.error(f"[Bot] CosmosDB write failed: {e}")

        await turn_context.send_activity(full_response)
        logger.info(
            f"[Bot] Response sent: conversation={conversation_id}, "
            f"sources={len(sources)}"
        )

    async def on_members_added_activity(self, members_added, turn_context: TurnContext) -> None:
        """Send a welcome message when the bot is added to a conversation."""
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                welcome = (
                    "Hello! I'm the **ETS VA Assistant**. Ask me questions about "
                    "internal policies, procedures, and documentation. "
                    "I'll search the knowledge base and provide answers with "
                    "**clickable source links** and citations."
                )
                await turn_context.send_activity(welcome)
                logger.info(f"[Bot] Welcome sent to member={member.id}")
