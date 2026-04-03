"""Entra ID SSO authentication for Chainlit UI.

Implements OIDC Authorization Code Flow with PKCE for browser-based authentication.
Uses MSAL (Microsoft Authentication Library) and signed session cookies.

Environment variables required:
    ENTRA_TENANT_ID: Azure AD tenant ID
    ENTRA_CLIENT_ID: App Registration client ID
    ENTRA_CLIENT_SECRET: App Registration client secret
    ENTRA_REDIRECT_URI: OAuth callback URL (e.g., https://<fqdn>/auth/callback)
    SESSION_SECRET: Secret key for encrypting session cookies
    AI_VA_ADMINS_GROUP_ID: Optional - Group OID for admins
"""

import logging
import os
import secrets
from typing import Optional, Dict
from urllib.parse import urlencode

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

# Environment configuration
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("ENTRA_CLIENT_SECRET", "")
ENTRA_REDIRECT_URI = os.environ.get("ENTRA_REDIRECT_URI", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
AI_VA_ADMINS_GROUP_ID = os.environ.get("AI_VA_ADMINS_GROUP_ID", "")

# Session cookie settings
SESSION_COOKIE_NAME = "m365_sso_session"
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", "28800"))  # 8 hours default
# Set secure=True only for HTTPS redirect URIs (allows HTTP for local dev)
SESSION_COOKIE_SECURE = ENTRA_REDIRECT_URI.startswith("https://")

# MSAL instance (lazy-initialized)
_msal_app = None


def get_msal_app():
    """Get or create the MSAL ConfidentialClientApplication."""
    global _msal_app
    if _msal_app is None:
        if not ENTRA_TENANT_ID or not ENTRA_CLIENT_ID or not ENTRA_CLIENT_SECRET:
            raise ValueError(
                "Entra ID SSO not configured. Set ENTRA_TENANT_ID, ENTRA_CLIENT_ID, "
                "ENTRA_CLIENT_SECRET, ENTRA_REDIRECT_URI, SESSION_SECRET environment variables."
            )

        import msal

        authority = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}"
        _msal_app = msal.ConfidentialClientApplication(
            ENTRA_CLIENT_ID,
            authority=authority,
            client_credential=ENTRA_CLIENT_SECRET,
        )
        logger.info(f"[Auth] MSAL initialized: tenant={ENTRA_TENANT_ID}, client_id={ENTRA_CLIENT_ID}")

    return _msal_app


def get_session_serializer() -> URLSafeTimedSerializer:
    """Get the session cookie serializer."""
    if not SESSION_SECRET:
        raise ValueError("SESSION_SECRET environment variable not set")
    return URLSafeTimedSerializer(SESSION_SECRET)


def create_session_cookie(user_data: Dict) -> str:
    """Create a signed session cookie containing user data.

    Args:
        user_data: Dict with keys: oid, name, email, groups (list of group OIDs)

    Returns:
        Signed cookie value
    """
    serializer = get_session_serializer()
    return serializer.dumps(user_data)


def read_session_cookie(cookie_value: str, max_age: int = SESSION_MAX_AGE) -> Optional[Dict]:
    """Read and validate a signed session cookie.

    Args:
        cookie_value: The session cookie value
        max_age: Maximum age in seconds (default: SESSION_MAX_AGE)

    Returns:
        User data dict if valid, None if invalid/expired
    """
    try:
        serializer = get_session_serializer()
        user_data = serializer.loads(cookie_value, max_age=max_age)
        return user_data
    except (BadSignature, SignatureExpired) as e:
        logger.warning(f"[Auth] Invalid/expired session cookie: {e}")
        return None


def get_user_from_request(request: Request) -> Optional[Dict]:
    """Extract authenticated user from request session cookie.

    Returns:
        User data dict with keys: oid, name, email, groups, role
        None if not authenticated
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return None

    user_data = read_session_cookie(cookie_value)
    if not user_data:
        return None

    # Check if user is admin
    groups = user_data.get("groups", [])
    is_admin = AI_VA_ADMINS_GROUP_ID and AI_VA_ADMINS_GROUP_ID in groups
    user_data["role"] = "admin" if is_admin else "user"

    return user_data


def build_auth_url(state: str) -> str:
    """Build the Entra ID authorization URL for OIDC login.

    Args:
        state: Random state value for CSRF protection

    Returns:
        Authorization URL to redirect user to
    """
    msal_app = get_msal_app()

    # OIDC scopes - openid and profile are automatically added by MSAL
    # Only specify additional scopes here
    scopes = ["User.Read"]

    auth_url = msal_app.get_authorization_request_url(
        scopes=scopes,
        state=state,
        redirect_uri=ENTRA_REDIRECT_URI,
    )

    return auth_url


def handle_callback(code: str, state: str) -> Optional[Dict]:
    """Handle OAuth callback and exchange authorization code for tokens.

    Args:
        code: Authorization code from Entra ID
        state: State value for validation

    Returns:
        User data dict with keys: oid, name, email, groups
        None if token exchange fails
    """
    msal_app = get_msal_app()

    # OIDC scopes - openid and profile are automatically included
    scopes = ["User.Read"]

    try:
        result = msal_app.acquire_token_by_authorization_code(
            code=code,
            scopes=scopes,
            redirect_uri=ENTRA_REDIRECT_URI,
        )

        if "error" in result:
            logger.error(f"[Auth] Token acquisition failed: {result.get('error')}: {result.get('error_description')}")
            return None

        # Extract claims from ID token
        id_token_claims = result.get("id_token_claims", {})

        user_data = {
            "oid": id_token_claims.get("oid"),  # Entra ID Object ID
            "name": id_token_claims.get("name", "Unknown User"),
            "email": id_token_claims.get("preferred_username") or id_token_claims.get("email"),
            "groups": id_token_claims.get("groups", []),  # List of group OIDs
        }

        # Handle group overage (>200 groups) - groups claim becomes _claim_names/_claim_sources
        if "_claim_names" in id_token_claims and "groups" in id_token_claims["_claim_names"]:
            logger.warning(
                f"[Auth] User {user_data['oid']} has >200 groups. Group overage not implemented yet. "
                "Admin features may not work. Implement Graph API call to resolve groups."
            )
            # TODO: Call Graph API to fetch user's groups
            # For now, set empty list
            user_data["groups"] = []

        logger.info(f"[Auth] User authenticated: oid={user_data['oid']}, email={user_data['email']}, groups={len(user_data['groups'])}")

        return user_data

    except Exception as e:
        logger.error(f"[Auth] Token exchange failed: {e}", exc_info=True)
        return None


def build_logout_url(post_logout_redirect_uri: str) -> str:
    """Build the Entra ID logout URL.

    Args:
        post_logout_redirect_uri: Where to redirect after logout

    Returns:
        Logout URL
    """
    params = {
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    logout_url = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/oauth2/v2.0/logout"
    return f"{logout_url}?{urlencode(params)}"


# FastAPI route handlers

def login_route(request: Request) -> RedirectResponse:
    """Initiate OIDC login flow.

    Generates a random state, stores it in a temporary cookie, and redirects to Entra ID.
    """
    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(state)

    response = RedirectResponse(url=auth_url)
    # Store state in a cookie for validation in callback
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=600,  # 10 minutes
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )

    logger.info(f"[Auth] Login initiated: state={state[:8]}...")
    return response


def callback_route(request: Request) -> RedirectResponse:
    """Handle OAuth callback from Entra ID.

    Exchanges authorization code for tokens, validates the ID token,
    creates a session cookie, and redirects to the app.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    # Check for OAuth errors
    if error:
        error_desc = request.query_params.get("error_description", "Unknown error")
        logger.error(f"[Auth] OAuth error: {error}: {error_desc}")
        return RedirectResponse(url="/auth/error?message=" + error_desc)

    if not code or not state:
        logger.error("[Auth] Missing code or state in callback")
        return RedirectResponse(url="/auth/error?message=Missing authorization code")

    # Validate state (CSRF protection)
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        logger.error(f"[Auth] State mismatch: stored={stored_state}, received={state}")
        return RedirectResponse(url="/auth/error?message=State validation failed")

    # Exchange code for tokens
    user_data = handle_callback(code, state)
    if not user_data:
        return RedirectResponse(url="/auth/error?message=Authentication failed")

    # Create session cookie
    session_value = create_session_cookie(user_data)

    response = RedirectResponse(url="/chat/")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",  # CSRF protection
    )

    # Clear the state cookie
    response.delete_cookie(key="oauth_state")

    logger.info(f"[Auth] Login successful: oid={user_data['oid']}, email={user_data['email']}")
    return response


def logout_route(request: Request) -> RedirectResponse:
    """Handle logout.

    Clears the session cookie and redirects to Entra ID logout endpoint
    to clear the SSO session.
    """
    # Build post-logout redirect (back to login)
    base_url = str(request.base_url).rstrip("/")
    post_logout_uri = f"{base_url}/chat/auth/login"

    logout_url = build_logout_url(post_logout_uri)

    response = RedirectResponse(url=logout_url)
    response.delete_cookie(key=SESSION_COOKIE_NAME)

    logger.info("[Auth] User logged out")
    return response
