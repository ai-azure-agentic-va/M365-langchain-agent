"""SSO authentication middleware for Chainlit UI routes."""

import logging

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from m365_langchain_agent.web.auth import (
    get_user_from_request,
    SESSION_COOKIE_NAME,
)

logger = logging.getLogger(__name__)


class SSOAuthMiddleware(BaseHTTPMiddleware):
    # Browser page loads → redirect to login. Internal Chainlit requests → pass through.

    _PASSTHROUGH_PREFIXES = (
        "/chat/ws/",
        "/chat/project/",
        "/chat/public/",
        "/chat/favicon",
        "/chat/files/",
        "/chat/auth/",
    )
    _PASSTHROUGH_EXACT = {
        "/chat/user",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path.startswith("/chat"):
            user = get_user_from_request(request)

            if user:
                request.state.user = user
                request.scope["headers"].append((b"x-user-oid", user["oid"].encode()))
                request.scope["headers"].append((b"x-user-name", user["name"].encode()))
                request.scope["headers"].append((b"x-user-email", (user.get("email") or "").encode()))
                request.scope["headers"].append((b"x-user-role", user["role"].encode()))

                response = await call_next(request)
                return response
            else:
                is_passthrough = (
                    path.startswith(self._PASSTHROUGH_PREFIXES)
                    or path in self._PASSTHROUGH_EXACT
                )
                if not is_passthrough:
                    return RedirectResponse(url="/chat/auth/login?next=/chat/&prompt=login")

        response = await call_next(request)
        return response
