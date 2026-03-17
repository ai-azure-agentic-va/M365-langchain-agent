"""FastAPI application — Bot Framework /api/messages endpoint + health checks.

This is the single entry point for the container. It:
    1. Receives Bot Framework Activity JSON at POST /api/messages
    2. Validates the incoming request using BotFrameworkAdapter
    3. Routes to DocAgentBot for processing
    4. Exposes /health and /readiness for K8s probes
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request, Response
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import Activity
from botframework.connector.auth import (
    AppCredentials,
    MicrosoftAppCredentials,
)

from m365_langchain_agent.bot import DocAgentBot
from m365_langchain_agent.agent import invoke_agent
from m365_langchain_agent.cosmos_store import get_cosmos_store


# ---------------------------------------------------------------------------
# Managed Identity credentials for UserAssignedMSI bots
# ---------------------------------------------------------------------------
class MsiAppCredentials(AppCredentials):
    """Bot credentials that use Azure Managed Identity instead of client secret.

    The botbuilder-python SDK doesn't natively support UserAssignedMSI.
    This class overrides token acquisition to use ManagedIdentityCredential.
    """

    def __init__(self, app_id: str, tenant_id: str = None):
        super().__init__(app_id=app_id, channel_auth_tenant=tenant_id)
        from azure.identity import ManagedIdentityCredential

        self._msi = ManagedIdentityCredential(client_id=app_id)
        logger.info(f"[Auth] Using ManagedIdentityCredential for app_id={app_id}")

    def get_access_token(self, force_refresh: bool = False) -> str:
        token = self._msi.get_token("https://api.botframework.com/.default")
        self.token = {"access_token": token.token, "token_type": "Bearer"}
        return token.token


class MsiBotFrameworkAdapter(BotFrameworkAdapter):
    """Adapter override that uses Managed Identity for outbound auth.

    The default adapter creates MicrosoftAppCredentials (client_secret based)
    for outbound calls via a name-mangled __get_app_credentials method.
    We monkey-patch it after construction to return MsiAppCredentials instead.
    """

    def __init__(self, settings: BotFrameworkAdapterSettings):
        super().__init__(settings)
        # Replace the name-mangled private method with our MSI version.
        # BotFrameworkAdapter.__get_app_credentials becomes
        # _BotFrameworkAdapter__get_app_credentials due to Python name mangling.
        self._BotFrameworkAdapter__get_app_credentials = self._msi_get_app_credentials

    async def _msi_get_app_credentials(self, app_id, scope, force=False):
        if not app_id:
            return MicrosoftAppCredentials.empty()

        cache_key = f"{app_id}{scope}"
        if cache_key in self._app_credential_map and not force:
            return self._app_credential_map[cache_key]

        credentials = MsiAppCredentials(
            app_id=app_id,
            tenant_id=self.settings.channel_auth_tenant,
        )
        self._app_credential_map[cache_key] = credentials
        return credentials


# ---------------------------------------------------------------------------
# Bot Framework Adapter
# ---------------------------------------------------------------------------
_app_id = os.environ.get("BOT_APP_ID", "")
_app_password = os.environ.get("BOT_APP_PASSWORD", "")
_auth_tenant = os.environ.get("BOT_AUTH_TENANT", None)

settings = BotFrameworkAdapterSettings(
    app_id=_app_id,
    app_password=_app_password,
    channel_auth_tenant=_auth_tenant,
)

# Use MSI adapter when app_id is set but no password (UserAssignedMSI mode)
if _app_id and not _app_password:
    logger.info("[App] UserAssignedMSI mode — using MsiBotFrameworkAdapter")
    adapter = MsiBotFrameworkAdapter(settings)
else:
    logger.info("[App] Standard mode — using BotFrameworkAdapter")
    adapter = BotFrameworkAdapter(settings)


# Error handler — log and attempt to notify user (best-effort)
async def on_error(context, error):
    logger.error(f"[Adapter] Unhandled error: {error}", exc_info=True)
    try:
        await context.send_activity("Sorry, something went wrong. Please try again.")
    except Exception:
        logger.warning("[Adapter] Could not send error message back to user")


adapter.on_turn_error = on_error

# Bot instance
bot = DocAgentBot()

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="M365 LangChain Agent",
    description="RAG agent with Bot Framework + CosmosDB",
    version="0.1.0",
)


@app.post("/api/messages")
async def messages(request: Request) -> Response:
    """Bot Framework messaging endpoint.

    Azure Bot Service sends Activity JSON here. The adapter validates
    the auth header, deserializes the Activity, and routes to our bot.
    """
    # Log every incoming request for debugging
    content_type = request.headers.get("Content-Type", "")
    auth_present = "Yes" if request.headers.get("Authorization") else "No"
    logger.info(f"[App] POST /api/messages — Content-Type={content_type}, Auth={auth_present}, Client={request.client.host}")

    if "application/json" not in content_type:
        logger.warning(f"[App] Rejected: unsupported Content-Type: {content_type}")
        return Response(status_code=415)

    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    logger.info(
        f"[App] Activity: type={activity.type}, "
        f"from={getattr(activity.from_property, 'id', 'N/A') if activity.from_property else 'N/A'}, "
        f"text={str(activity.text or '')[:80]}"
    )

    try:
        response = await adapter.process_activity(activity, auth_header, bot.on_turn)
        if response:
            logger.info(f"[App] Response: status={response.status}")
            return Response(
                content=response.body,
                status_code=response.status,
                headers=response.headers,
            )
        logger.info("[App] Response: 201 (no body)")
        return Response(status_code=201)
    except Exception as e:
        logger.error(f"[App] Failed to process activity: {e}", exc_info=True)
        return Response(status_code=500, content="Internal server error")


DEPLOY_TARGET = os.environ.get("DEPLOY_TARGET", "CONTAINER_APPS").upper().strip()


@app.get("/health")
async def health():
    """Health check for liveness probe."""
    return {
        "status": "healthy",
        "service": "m365-langchain-agent",
        "deploy_target": DEPLOY_TARGET,
    }


@app.get("/readiness")
async def readiness():
    """Readiness check for readiness probe."""
    return {
        "status": "ready",
        "service": "m365-langchain-agent",
        "deploy_target": DEPLOY_TARGET,
    }


@app.post("/test/query")
async def test_query(request: Request):
    """Test endpoint — bypasses Bot Framework auth to verify full RAG pipeline.

    Send: {"query": "your question here", "conversation_id": "test-123"}
    Returns the agent answer + pipeline status.
    Remove this endpoint before production.
    """
    body = await request.json()
    query = body.get("query", "")
    conversation_id = body.get("conversation_id", "test-session")
    model_name = body.get("model")
    top_k = body.get("top_k")
    temperature = body.get("temperature")
    filter_expr = body.get("filter")

    if not query:
        return {"error": "Missing 'query' field"}

    results = {"query": query, "conversation_id": conversation_id, "steps": {}}
    if model_name:
        results["model"] = model_name

    # Step 1: CosmosDB — load history
    try:
        cosmos = get_cosmos_store()
        history = cosmos.get_history(conversation_id)
        results["steps"]["cosmos_read"] = {"status": "ok", "history_length": len(history)}
    except Exception as e:
        results["steps"]["cosmos_read"] = {"status": "error", "error": str(e)}
        history = []

    # Step 2: Agent — search + generate
    try:
        agent_result = await invoke_agent(
            query=query,
            conversation_history=history,
            model_name=model_name,
            top_k=int(top_k) if top_k else None,
            temperature=float(temperature) if temperature is not None else None,
            filter_expr=filter_expr,
        )
        answer = agent_result["answer"]
        sources = agent_result["sources"]
        results["steps"]["agent"] = {
            "status": "ok",
            "answer_length": len(answer),
            "source_count": len(sources),
        }
        results["answer"] = answer
        results["sources"] = sources
    except Exception as e:
        results["steps"]["agent"] = {"status": "error", "error": str(e)}
        results["answer"] = None
        results["sources"] = []
        return results

    # Step 3: CosmosDB — save turn
    try:
        cosmos = get_cosmos_store()
        cosmos.save_turn(conversation_id=conversation_id, user_message=query, bot_response=answer)
        results["steps"]["cosmos_write"] = {"status": "ok"}
    except Exception as e:
        results["steps"]["cosmos_write"] = {"status": "error", "error": str(e)}

    return results


@app.get("/")
async def root():
    """Root endpoint — basic service info."""
    return {
        "service": "m365-langchain-agent",
        "version": "0.1.0",
        "endpoints": {
            "messages": "/api/messages",
            "health": "/health",
            "readiness": "/readiness",
        },
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    logger.info(f"[App] Starting M365 LangChain Agent on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
