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
        """Handle incoming user messages.

        Flow:
            1. Extract user text and identity from the Activity
            2. Load conversation history from CosmosDB
            3. Invoke the LangChain RAG agent
            4. Format answer with clickable source links
            5. Save the turn to CosmosDB (with user identity)
            6. Send the response back to the user
        """
        user_text = turn_context.activity.text
        if not user_text or not user_text.strip():
            await turn_context.send_activity("I didn't receive a message. Please try again.")
            return

        conversation_id = turn_context.activity.conversation.id

        # Extract user identity from Teams (SSO)
        # Teams provides aad_object_id in the from_property of the Activity
        user_id = None
        user_name = None
        user_email = None

        if turn_context.activity.from_property:
            # aad_object_id is the Entra ID Object ID — available for Teams users
            user_id = getattr(turn_context.activity.from_property, "aad_object_id", None)
            user_name = getattr(turn_context.activity.from_property, "name", None)

            # Email is not directly available in from_property, but we can use the ID as a fallback
            # For a full profile, we'd need to call Graph API, but aad_object_id is sufficient
            # for user scoping and audit trail
            user_email = None  # Not available from Activity alone

        logger.info(
            f"[Bot] Message received: conversation={conversation_id}, "
            f"user_id={user_id or 'anonymous'}, text={user_text[:100]}"
        )

        # Send typing indicator while processing
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        # Load conversation history from CosmosDB (with user validation)
        try:
            cosmos = get_cosmos_store()
            history = cosmos.get_history(conversation_id, user_id=user_id)
        except Exception as e:
            logger.error(f"[Bot] CosmosDB read failed: {e}")
            history = []

        # Invoke the LangChain RAG agent — returns structured AgentResult
        result = await invoke_agent(query=user_text, conversation_history=history)
        answer = result["answer"]
        sources = result["sources"]

        # Format sources as markdown links (Teams renders markdown)
        sources_md = format_sources_markdown(sources)
        if sources_md:
            full_response = f"{answer}\n\n---\n**Sources:**\n{sources_md}"
        else:
            full_response = answer

        # Save only the answer text (not sources footer) to CosmosDB
        # so conversation history stays clean for query rewriting
        # Include user identity for per-user history and audit trail
        try:
            cosmos = get_cosmos_store()
            cosmos.save_turn(
                conversation_id=conversation_id,
                user_message=user_text,
                bot_response=answer,
                user_id=user_id,
                user_email=user_email,
                user_display_name=user_name,
            )
        except Exception as e:
            logger.error(f"[Bot] CosmosDB write failed: {e}")

        # Send the response back
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
