"""FastAPI application factory with async lifespan management.

Creates the app, registers routes, optionally mounts Chainlit UI
and SSO middleware based on USER_INTERFACE setting.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from starlette.middleware.base import BaseHTTPMiddleware

from m365_langchain_agent import __version__
from m365_langchain_agent.config import settings
from m365_langchain_agent.log_config import setup_logging, set_request_id
from m365_langchain_agent.cosmos import get_cosmos_store, close_cosmos_store
from m365_langchain_agent.core.search import get_search_client, close_search_client
from m365_langchain_agent.web.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialize async clients. Shutdown: close them."""
    setup_logging(level=settings.log_level)
    logger.info("Starting m365-langchain-agent v%s", __version__)

    await get_cosmos_store()
    await get_search_client()
    logger.info("Async clients initialized")

    yield

    await close_search_client()
    await close_cosmos_store()
    logger.info("Async clients closed")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns a unique request ID to every inbound request for log correlation."""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or None
        rid = set_request_id(rid)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="M365 LangChain Agent",
        description="RAG agent with Bot Framework, Chainlit UI, and CosmosDB",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)

    user_interface = settings.user_interface.upper().strip()

    if user_interface == "CHAINLIT_UI":
        from chainlit.utils import mount_chainlit

        if settings.enable_sso:
            from m365_langchain_agent.web.middleware import SSOAuthMiddleware
            app.add_middleware(SSOAuthMiddleware)
            logger.info("SSO middleware enabled")

        chainlit_target = os.path.join(
            os.path.dirname(__file__), "chainlit_app.py"
        )
        mount_chainlit(app=app, target=chainlit_target, path="/chat")

        # Replace root with redirect to /chat/
        app.routes[:] = [r for r in app.routes if not (hasattr(r, "path") and r.path == "/")]

        @app.get("/", include_in_schema=False)
        async def root_redirect():
            return RedirectResponse(url="/chat/")

    elif user_interface == "BOT_SERVICE":
        @app.get("/")
        async def root():
            return {
                "service": "m365-langchain-agent",
                "version": __version__,
                "endpoints": {
                    "messages": "/api/messages",
                    "health": "/health",
                    "readiness": "/readiness",
                },
            }
    else:
        logger.error("Unknown USER_INTERFACE='%s'. Use CHAINLIT_UI or BOT_SERVICE.", user_interface)
        sys.exit(1)

    return app
