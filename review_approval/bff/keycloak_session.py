"""
Real Keycloak session auth for the /ui/* HTMX UI (Authorization Code
flow) -- replaces the earlier bff/mock_auth.py (no password, trusted
whatever role a session cookie claimed).

Session shape: {"username", "role", "access_token", "refresh_token",
"id_token", "expires_at"}.

`role` ("operator"/"manager", lowercase) is a **Phase 2 bridge, not the
real authorization mechanism** -- it's derived once at login time from
the token's realm_access.roles claim (Operator/Manager are still plain
realm roles; see keycloak/INTEGRATION_PLAN.md), purely so bff/ui.py's
existing require_session_role() dependency keeps working unchanged
until Phase 3 replaces it with real permission checks via
workflow/keycloak_auth.get_permissions(). Once that lands, this field
goes away -- don't build anything new on top of it.

No token refresh yet (see keycloak/INTEGRATION_PLAN.md's Phase 2 open
item) -- access tokens are short-lived (5 min, confirmed against this
project's realm); once expired, the session is simply treated as
logged-out and the user re-authenticates. Simple and correct, if not
maximally convenient -- a reasonable trade for a POC.
"""

import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

from review_approval.workflow import keycloak_auth

SESSION_KEY = "user"
_STATE_KEY = "oauth_state"


class RequireLoginRedirect(HTTPException):
    """Raised when no session exists; app.py registers a handler that
    turns this into a 303 redirect to the login page."""

    def __init__(self) -> None:
        super().__init__(status_code=303, headers={"Location": "/ui/login"})


def _issuer() -> str:
    issuer = os.environ.get("KEYCLOAK_ISSUER")
    if not issuer:
        raise RuntimeError("KEYCLOAK_ISSUER is not set")
    return issuer


def _client_id() -> str:
    client_id = os.environ.get("KEYCLOAK_CLIENT_ID")
    if not client_id:
        raise RuntimeError("KEYCLOAK_CLIENT_ID is not set")
    return client_id


def _client_secret() -> str:
    secret = os.environ.get("KEYCLOAK_CLIENT_SECRET")
    if not secret:
        raise RuntimeError("KEYCLOAK_CLIENT_SECRET is not set")
    return secret


def _redirect_uri(request: Request) -> str:
    # Built from the incoming request's own host:port rather than a
    # hardcoded value, so this works whether bff is native (localhost)
    # or Dockerized (whatever host it's actually reached on) -- must
    # exactly match a redirectUri registered on the "review-approval"
    # client in keycloak/import/myrealm-realm.json.
    return str(request.base_url) + "ui/callback"


def build_authorize_url(request: Request) -> str:
    """Generates a fresh CSRF state, stashes it in the session, and
    returns the Keycloak authorize URL to send the browser to."""
    state = secrets.token_urlsafe(24)
    request.session[_STATE_KEY] = state
    params = {
        "client_id": _client_id(),
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": _redirect_uri(request),
        "state": state,
    }
    return f"{_issuer()}/protocol/openid-connect/auth?{urlencode(params)}"


async def complete_login(request: Request, code: str, state: str) -> None:
    """Exchange an authorization code for tokens, validate the returned
    access token, and store the session.

    Raises ValueError on any failure (state mismatch, code exchange
    rejected, no Operator/Manager role) -- callers map that to a
    re-rendered login page with an error, matching the old
    mock_auth.py's ValueError convention for login failures.
    """
    expected_state = request.session.pop(_STATE_KEY, None)
    if not expected_state or expected_state != state:
        raise ValueError("Login session expired or was tampered with -- try again.")

    token_url = f"{_issuer()}/protocol/openid-connect/token"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request),
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
        )
    if response.status_code != 200:
        raise ValueError(f"Keycloak rejected the login: {response.text}")
    tokens = response.json()

    try:
        claims = keycloak_auth.decode_token(tokens["access_token"])
    except Exception as e:
        raise ValueError(f"Keycloak issued a token that failed validation: {e}")

    roles = set(claims.get("realm_access", {}).get("roles", []))
    if "Operator" in roles:
        role = "operator"
    elif "Manager" in roles:
        role = "manager"
    else:
        raise ValueError(
            "This account has neither the Operator nor Manager role -- "
            "contact an admin."
        )

    # Deliberately NOT storing refresh_token/id_token here. Starlette's
    # SessionMiddleware signs the whole session into ONE browser cookie
    # (itsdangerous, not a server-side store) -- access_token alone is
    # already ~1.4KB, and all three JWTs together measured ~4.5KB signed,
    # over the ~4KB limit real browsers enforce per cookie (confirmed by
    # actually measuring it during development, not assumed -- curl
    # itself doesn't enforce that limit, so a curl-only test would have
    # silently passed while a real browser truncated or rejected the
    # cookie). Neither is used by any code yet anyway (no token refresh
    # this phase; logout falls back to a client_id-based end-session
    # call below instead of id_token_hint). Revisit if that changes --
    # e.g. move to a server-side session store rather than growing this
    # cookie further.
    request.session[SESSION_KEY] = {
        "username": claims.get("preferred_username", claims.get("sub")),
        "role": role,
        "access_token": tokens["access_token"],
        "expires_at": time.time() + tokens.get("expires_in", 300),
    }


def logout_redirect_url(request: Request) -> str:
    """URL to send the browser to for a real Keycloak single-logout --
    ends Keycloak's own session too, not just this app's cookie.

    Uses client_id (not id_token_hint) to identify the client, since the
    session doesn't hold an id_token -- see complete_login()'s docstring
    for why. Requires post_logout_redirect_uri to match what's
    registered on the client (redirectUris in
    keycloak/import/myrealm-realm.json also covers this per Keycloak's
    RP-initiated logout support for the client_id+redirect_uri pattern).
    """
    post_logout_redirect_uri = str(request.base_url) + "ui/login"
    params = {
        "client_id": _client_id(),
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    return f"{_issuer()}/protocol/openid-connect/logout?{urlencode(params)}"


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def get_session_user(request: Request) -> dict:
    user = request.session.get(SESSION_KEY)
    if not user:
        raise RequireLoginRedirect()
    if time.time() >= user.get("expires_at", 0):
        # Access token expired -- no refresh yet (a known Phase 2
        # limitation, see this module's docstring); treat as logged out
        # rather than silently using a token Keycloak would reject.
        request.session.pop(SESSION_KEY, None)
        raise RequireLoginRedirect()
    return user


def require_session_role(role: str):
    """FastAPI dependency factory: require_session_role("operator")

    Phase 2 bridge -- see this module's docstring. Superseded in
    Phase 3 by real permission checks.
    """

    def checker(request: Request) -> dict:
        user = get_session_user(request)
        if user["role"] != role:
            raise HTTPException(status_code=403, detail=f"requires role: {role}")
        return user

    return checker
