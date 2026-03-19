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
from m365_langchain_agent.metrics_store import get_metrics_store
from m365_langchain_agent.content_safety import run_all_evaluations, CONTENT_SAFETY_ENABLED

logger = logging.getLogger(__name__)


class DocAgentBot(ActivityHandler):
    """Bot that handles incoming messages and routes them to the RAG agent."""

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Handle incoming user messages.

        Flow:
            1. Extract user text from the Activity
            2. Load conversation history from CosmosDB
            3. Invoke the LangChain RAG agent
            4. Format answer with clickable source links
            5. Save the turn to CosmosDB
            6. Send the response back to the user
        """
        user_text = turn_context.activity.text
        if not user_text or not user_text.strip():
            await turn_context.send_activity("I didn't receive a message. Please try again.")
            return

        conversation_id = turn_context.activity.conversation.id
        logger.info(
            f"[Bot] Message received: conversation={conversation_id}, "
            f"text={user_text[:100]}"
        )

        # Send typing indicator while processing
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        # Load conversation history from CosmosDB
        try:
            cosmos = get_cosmos_store()
            history = cosmos.get_history(conversation_id)
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
        try:
            cosmos = get_cosmos_store()
            cosmos.save_turn(
                conversation_id=conversation_id,
                user_message=user_text,
                bot_response=answer,
            )
        except Exception as e:
            logger.error(f"[Bot] CosmosDB write failed: {e}")

        # Run content safety evaluations (backend only)
        safety_results = {}
        if CONTENT_SAFETY_ENABLED and result.get("raw_chunks"):
            try:
                context = "\n\n".join(c.get("content", "") for c in result["raw_chunks"])
                safety_results = await run_all_evaluations(
                    query=user_text, answer=answer, context=context
                )
            except Exception as e:
                logger.error(f"[Bot] Content safety evaluation failed: {e}")

        # Save metrics (token usage, groundedness, safety)
        try:
            metrics = get_metrics_store()
            metrics.save_metrics(
                conversation_id=conversation_id,
                query=user_text,
                model="default",
                token_usage=result.get("token_usage", {}),
                content_safety=safety_results if safety_results else None,
            )
        except Exception as e:
            logger.error(f"[Bot] Metrics save failed: {e}")

        # Send the response back
        token_usage = result.get("token_usage", {})
        await turn_context.send_activity(full_response)
        logger.info(
            f"[Bot] Response sent: conversation={conversation_id}, "
            f"sources={len(sources)}, tokens={token_usage.get('total_tokens', 0)}"
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
