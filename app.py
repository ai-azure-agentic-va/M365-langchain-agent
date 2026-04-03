"""Single entry point for the container.

Modes (set USER_INTERFACE env var):
    CHAINLIT_UI  → Mounts Chainlit web chat at /chat, redirects / → /chat
    BOT_SERVICE  → Exposes /api/messages for Bot Framework (default)

Both modes share /health, /readiness, /test/query endpoints.
"""

import logging
import os
import sys
import json

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
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


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment flag from common truthy values."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


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


@app.get("/health")
async def health():
    """Health check for liveness probe."""
    return {
        "status": "healthy",
        "service": "m365-langchain-agent",
    }


@app.get("/readiness")
async def readiness():
    """Readiness check for readiness probe."""
    return {
        "status": "ready",
        "service": "m365-langchain-agent",
    }


@app.get("/starter-prompts")
async def starter_prompts():
    """Return starter prompts configured via env flags and STARTER_PROMPTS JSON."""
    if not _env_flag("SHOW_STARTER_PROMPTS", default=True):
        return {"prompts": []}

    raw = os.environ.get("STARTER_PROMPTS", "").strip()
    if not raw:
        return {"prompts": []}
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[App] STARTER_PROMPTS is not valid JSON")
        return {"prompts": []}
    prompts = [item.get("message", "").strip() for item in items if isinstance(item, dict)]
    prompts = [p for p in prompts if p]
    return {"prompts": prompts}


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
        results["raw_chunks"] = agent_result.get("raw_chunks", [])
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


# ---------------------------------------------------------------------------
# SSO / Authentication (Chainlit UI only)
# ---------------------------------------------------------------------------

@app.get("/chat/auth/login")
async def auth_login(request: Request):
    """Initiate Entra ID SSO login flow."""
    from m365_langchain_agent.auth import login_route
    return login_route(request)


@app.get("/chat/auth/callback")
async def auth_callback(request: Request):
    """Handle OAuth callback from Entra ID."""
    from m365_langchain_agent.auth import callback_route
    return callback_route(request)


@app.get("/chat/auth/logout")
async def auth_logout(request: Request):
    """Logout and clear SSO session."""
    from m365_langchain_agent.auth import logout_route
    return logout_route(request)


@app.get("/chat/auth/error")
async def auth_error(request: Request):
    """Auth error page."""
    message = request.query_params.get("message", "Authentication failed")
    return Response(
        content=f"<html><body><h1>Authentication Error</h1><p>{message}</p><p><a href='/chat/auth/login'>Try again</a></p></body></html>",
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# SSO Middleware (protects /chat/ routes in Chainlit UI mode)
# ---------------------------------------------------------------------------

class SSOAuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces SSO authentication for Chainlit UI routes.

    - Browser page loads (/chat/, /chat): redirect to /auth/login if no session cookie.
    - Internal Chainlit requests (WebSocket, Socket.IO, APIs, assets): inject user
      headers if cookie is present, otherwise pass through — Chainlit's
      header_auth_callback will fall back to default-user.
    """

    # Paths that are internal Chainlit plumbing — never redirect these
    _PASSTHROUGH_PREFIXES = (
        "/chat/ws/",          # WebSocket / Socket.IO
        "/chat/project/",     # Chainlit project config (translations, etc.)
        "/chat/public/",      # Static assets (CSS, JS, images)
        "/chat/favicon",      # Favicon
        "/chat/files/",       # Uploaded files
        "/chat/auth/",        # SSO auth routes (login, callback, logout, error)
    )
    # Exact paths that are internal Chainlit API
    _PASSTHROUGH_EXACT = {
        "/chat/user",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only act on /chat/ routes
        if path.startswith("/chat"):
            from m365_langchain_agent.auth import (
                get_user_from_request, create_session_cookie,
                SESSION_COOKIE_NAME, SESSION_IDLE_TIMEOUT, SESSION_COOKIE_SECURE,
            )

            user = get_user_from_request(request)

            if user:
                # Authenticated — inject user identity as custom headers for Chainlit
                request.state.user = user
                request.scope["headers"].append((b"x-user-oid", user["oid"].encode()))
                request.scope["headers"].append((b"x-user-name", user["name"].encode()))
                request.scope["headers"].append((b"x-user-email", (user.get("email") or "").encode()))
                request.scope["headers"].append((b"x-user-role", user["role"].encode()))

                # Re-issue cookie to reset idle timeout clock
                response = await call_next(request)
                cookie_data = {k: v for k, v in user.items() if k != "role"}
                response.set_cookie(
                    key=SESSION_COOKIE_NAME,
                    value=create_session_cookie(cookie_data),
                    max_age=SESSION_IDLE_TIMEOUT,
                    httponly=True,
                    secure=SESSION_COOKIE_SECURE,
                    samesite="lax",
                )
                return response
            else:
                # Not authenticated — only redirect browser page navigations,
                # never redirect WebSocket, Socket.IO, or Chainlit API requests
                is_passthrough = (
                    path.startswith(self._PASSTHROUGH_PREFIXES)
                    or path in self._PASSTHROUGH_EXACT
                )
                if not is_passthrough:
                    return RedirectResponse(url="/chat/auth/login?next=/chat/")

        response = await call_next(request)
        return response


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    user_interface = os.environ.get("USER_INTERFACE", "BOT_SERVICE").upper().strip()

    logger.info(f"[App] USER_INTERFACE={user_interface}")

    if user_interface == "CHAINLIT_UI":
        logger.info(f"[App] Starting Chainlit UI on port {port}")
        from chainlit.utils import mount_chainlit

        # Add SSO middleware to protect /chat/ routes
        enable_sso = os.environ.get("ENABLE_SSO", "true").lower().strip() == "true"
        if enable_sso:
            logger.info("[App] SSO enabled — adding authentication middleware")
            app.add_middleware(SSOAuthMiddleware)
        else:
            logger.warning("[App] SSO disabled (ENABLE_SSO=false) — running without authentication")

        chainlit_target = os.path.join(
            os.path.dirname(__file__), "m365_langchain_agent", "chainlit_app.py"
        )
        mount_chainlit(app=app, target=chainlit_target, path="/chat")

        # Replace root route with redirect to Chainlit UI
        app.routes[:] = [r for r in app.routes if not (hasattr(r, "path") and r.path == "/")]

        @app.get("/", include_in_schema=False)
        async def root_redirect():
            return RedirectResponse(url="/chat/")

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )

    elif user_interface == "BOT_SERVICE":
        logger.info(f"[App] Starting Bot Service on port {port}")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )

    else:
        logger.error(f"[App] Unknown USER_INTERFACE='{user_interface}'. Use CHAINLIT_UI or BOT_SERVICE.")
        sys.exit(1)
