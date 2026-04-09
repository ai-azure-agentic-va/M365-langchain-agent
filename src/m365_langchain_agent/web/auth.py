"""Entra ID SSO authentication for Chainlit UI.

Implements OIDC Authorization Code Flow with MSAL and signed session cookies.
"""

import logging
import secrets

from urllib.parse import urlencode

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request
from fastapi.responses import RedirectResponse

from m365_langchain_agent.config import settings
from m365_langchain_agent.key_vault import get_entra_client_secret

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "m365_sso_session"
SIGNED_OUT_COOKIE_NAME = "m365_sso_signed_out"
CHAINLIT_SESSION_COOKIE_NAME = "X-Chainlit-Session-id"

_msal_app = None


def get_msal_app():
    """Get or create the MSAL ConfidentialClientApplication."""
    global _msal_app
    if _msal_app is None:
        try:
            client_secret = get_entra_client_secret()
        except ValueError as e:
            raise ValueError(
                f"Failed to get Entra client secret: {e}. "
                "Set ENTRA_CLIENT_SECRET or configure Key Vault."
            )

        if not settings.entra_tenant_id or not settings.entra_client_id or not client_secret:
            raise ValueError(
                "Entra ID SSO not configured. Set ENTRA_TENANT_ID, ENTRA_CLIENT_ID, "
                "and ENTRA_CLIENT_SECRET."
            )

        import msal

        authority = f"https://login.microsoftonline.com/{settings.entra_tenant_id}"
        _msal_app = msal.ConfidentialClientApplication(
            settings.entra_client_id,
            authority=authority,
            client_credential=client_secret,
        )
        logger.info("MSAL initialized: tenant=%s, client=%s", settings.entra_tenant_id, settings.entra_client_id)

    return _msal_app


def get_session_serializer() -> URLSafeTimedSerializer:
    if not settings.session_secret:
        raise ValueError("SESSION_SECRET not set")
    return URLSafeTimedSerializer(settings.session_secret)


def create_session_cookie(user_data: dict) -> str:
    return get_session_serializer().dumps(user_data)


def read_session_cookie(cookie_value: str, max_age: int | None = None) -> dict | None:
    if max_age is None:
        max_age = settings.session_idle_timeout
    try:
        return get_session_serializer().loads(cookie_value, max_age=max_age)
    except (BadSignature, SignatureExpired) as e:
        logger.warning("Invalid/expired session cookie: %s", e)
        return None


def get_user_from_request(request: Request) -> dict | None:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return None

    user_data = read_session_cookie(cookie_value)
    if not user_data:
        return None

    groups = user_data.get("groups", [])
    is_admin = settings.ai_va_admins_group_id and settings.ai_va_admins_group_id in groups
    user_data["role"] = "admin" if is_admin else "user"
    return user_data


def build_auth_url(state: str, prompt: str | None = None) -> str:
    msal_app = get_msal_app()
    return msal_app.get_authorization_request_url(
        scopes=["User.Read"],
        state=state,
        redirect_uri=settings.entra_redirect_uri,
        prompt=prompt,
    )


def handle_callback(code: str, state: str) -> dict | None:
    msal_app = get_msal_app()
    try:
        result = msal_app.acquire_token_by_authorization_code(
            code=code,
            scopes=["User.Read"],
            redirect_uri=settings.entra_redirect_uri,
        )

        if "error" in result:
            logger.error("Token acquisition failed: %s: %s", result.get("error"), result.get("error_description"))
            return None

        claims = result.get("id_token_claims", {})
        user_data = {
            "oid": claims.get("oid"),
            "name": claims.get("name", "Unknown User"),
            "email": claims.get("preferred_username") or claims.get("email"),
            "groups": claims.get("groups", []),
        }

        if "_claim_names" in claims and "groups" in claims["_claim_names"]:
            logger.warning("User %s has >200 groups — group overage not implemented", user_data["oid"])
            user_data["groups"] = []

        logger.info("User authenticated: oid=%s, email=%s", user_data["oid"], user_data["email"])
        return user_data
    except Exception as e:
        logger.error("Token exchange failed: %s", e, exc_info=True)
        return None


def build_logout_url(post_logout_redirect_uri: str) -> str:
    params = {
        "client_id": settings.entra_client_id,
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    return f"https://login.microsoftonline.com/{settings.entra_tenant_id}/oauth2/v2.0/logout?{urlencode(params)}"


def login_route(request: Request) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    prompt = request.query_params.get("prompt")
    was_signed_out = request.cookies.get(SIGNED_OUT_COOKIE_NAME)
    if was_signed_out and not prompt:
        prompt = "login"

    auth_url = build_auth_url(state, prompt=prompt)
    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


def callback_route(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        error_desc = request.query_params.get("error_description", "Unknown error")
        logger.error("OAuth error: %s: %s", error, error_desc)
        return RedirectResponse(url="/chat/auth/error?message=" + error_desc)

    if not code or not state:
        return RedirectResponse(url="/chat/auth/error?message=Missing authorization code")

    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        return RedirectResponse(url="/chat/auth/error?message=State validation failed")

    user_data = handle_callback(code, state)
    if not user_data:
        return RedirectResponse(url="/chat/auth/error?message=Authentication failed")

    session_value = create_session_cookie(user_data)
    response = RedirectResponse(url="/chat/")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=settings.session_idle_timeout,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )

    for key in ["oauth_state", SIGNED_OUT_COOKIE_NAME, settings.chainlit_auth_cookie_name, CHAINLIT_SESSION_COOKIE_NAME]:
        response.delete_cookie(key=key)
        response.delete_cookie(key=key, path="/")

    for key in [settings.chainlit_auth_cookie_name, CHAINLIT_SESSION_COOKIE_NAME]:
        response.delete_cookie(key=key, path="/chat")

    return response


def logout_route(request: Request) -> RedirectResponse:
    base_url = str(request.base_url).rstrip("/")
    logout_url = build_logout_url(f"{base_url}/chat/auth/signed-out")

    response = RedirectResponse(url=logout_url)

    for key in [SESSION_COOKIE_NAME, "oauth_state", settings.chainlit_auth_cookie_name, CHAINLIT_SESSION_COOKIE_NAME]:
        response.delete_cookie(key=key)

    response.set_cookie(
        key=SIGNED_OUT_COOKIE_NAME,
        value="1",
        max_age=3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )

    for key in [SESSION_COOKIE_NAME, "oauth_state"]:
        response.delete_cookie(key=key, path="/", secure=settings.session_cookie_secure, httponly=True, samesite="lax")

    for key in [settings.chainlit_auth_cookie_name, CHAINLIT_SESSION_COOKIE_NAME]:
        response.delete_cookie(key=key, path="/")
        response.delete_cookie(key=key, path="/chat")

    logger.info("User logged out, cookies cleared")
    return response
