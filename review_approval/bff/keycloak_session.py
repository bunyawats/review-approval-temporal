"""
Real Keycloak session auth for the /ui/* HTMX UI (Authorization Code
flow) -- replaces the earlier bff/mock_auth.py (no password, trusted
whatever role a session cookie claimed).

Session shape (see docs/SESSION_STORE_PLAN.md): the browser cookie under
SESSION_KEY holds only an opaque server-side session id, minted at login
(session_store.new_session_id()); everything else lives in Redis under
that id (session_store.py) -- {"username", "role", "access_token",
"access_expires_at", "refresh_token", "refresh_expires_at"}. This used to
be the full dict inline in the (signed, but not encrypted) cookie
itself, deliberately without refresh_token/id_token -- all three tokens
together measured ~4.5KB signed, over the ~4KB limit real browsers
enforce per cookie. Moving the token set server-side removes that
constraint entirely (nothing sensitive ever reaches the browser, and
there's no size ceiling on what Redis can hold), which is what makes
storing refresh_token -- and therefore real token refresh -- possible.

Two authorization mechanisms, for two different purposes -- don't
conflate them:

- **`require_session_role(role)`** gates *page/screen selection*
  ("operator" vs "manager"), not specific actions -- `Operator`/
  `Manager` are still plain realm roles (see
  keycloak/import/myrealm-realm.json), and there's no Resource/
  Permission for "which screen can I see", so a role check is the
  right tool here, same as the REST API's `get_current_user()` gating
  its identity-only GET routes by nothing more than "is this a valid
  session" -- not a Phase 2 stopgap, a permanent, deliberate choice.
- **`require_permission(permission)`** / **`check_permission(user,
  permission)`** gate the five *mutating* actions (Create, Update,
  Cancel, Approve, Reject -- Scopes on the single "RequestApproval"
  Resource) via a real UMA ticket exchange
  (workflow/keycloak_auth.get_permissions()) -- the same mechanism
  api/auth.py uses for the REST API. Added in Phase 3; see
  keycloak/INTEGRATION_PLAN.md.

Access-token refresh happens lazily, inside `get_session_user()` (the
single choke point both mechanisms above call through): an expired
`access_token` triggers a `workflow.keycloak_auth.refresh_access_token()`
call using the stored `refresh_token`, transparent to the caller. Only
when the refresh token itself is rejected (past this realm's 30-minute
`ssoSessionIdleTimeout`, or revoked) does the session actually end,
falling back to a re-login -- same outcome as today, just at the
refresh-token's expiry rather than the much shorter access-token's.
"""

import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from redis.exceptions import RedisError

from review_approval.bff import session_store
from review_approval.workflow import keycloak_auth, memory_service

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


async def complete_login(request: Request, code: str, state: str) -> str:
    """Exchange an authorization code for tokens, validate the returned
    access token, and store the session. Returns the session's role
    ("operator"/"manager") so callers can redirect without reaching into
    session/Redis internals themselves.

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

    # Full token set (including refresh_token, needed for get_session_user()'s
    # transparent refresh) goes to Redis under a freshly minted session id;
    # the cookie itself holds only that id -- see this module's docstring
    # for why (cookie-size limit, no longer a constraint on the token set
    # since it doesn't live in the cookie at all now).
    session_id = session_store.new_session_id()
    await session_store.set(
        request.app.state.redis,
        session_id,
        {
            "username": claims.get("preferred_username", claims.get("sub")),
            "role": role,
            "access_token": tokens["access_token"],
            "access_expires_at": time.time() + tokens.get("expires_in", 300),
            "refresh_token": tokens.get("refresh_token"),
            "refresh_expires_at": time.time() + tokens.get("refresh_expires_in", 0),
        },
    )
    request.session[SESSION_KEY] = session_id
    return role


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


async def logout(request: Request) -> None:
    session_id = request.session.pop(SESSION_KEY, None)
    if session_id:
        r = request.app.state.redis
        try:
            await session_store.delete(r, session_id)
        except RedisError:
            # Cookie is already cleared, which is what actually ends the
            # browser's session; a stray Redis entry left behind just
            # expires on its own TTL (SESSION_TTL_SECONDS) rather than
            # needing this call to succeed.
            pass
        try:
            await memory_service.SessionMemory.delete(r, session_id)
        except RedisError:
            # Same reasoning as above -- a stray ui-memory:<id> entry is
            # harmless, it just expires on its own TTL.
            pass


async def get_session_user(request: Request) -> dict:
    session_id = request.session.get(SESSION_KEY)
    if not session_id:
        raise RequireLoginRedirect()

    r = request.app.state.redis
    try:
        user = await session_store.get(r, session_id)
    except RedisError:
        # Session store unreachable -- fail closed to "please log in
        # again" rather than a raw 500. A transient Redis outage
        # shouldn't crash every /ui/* route; it should look like an
        # ordinary expired session.
        raise RequireLoginRedirect()
    if not user:
        # Never existed, evicted, or expired via Redis's own TTL -- same
        # outward behavior as today's missing-cookie case.
        raise RequireLoginRedirect()

    if time.time() >= user.get("access_expires_at", 0):
        try:
            refreshed = await keycloak_auth.refresh_access_token(user["refresh_token"])
        except keycloak_auth.RefreshFailed:
            # Refresh token itself is no longer good (past the 30-minute
            # idle window, or revoked) -- this is a real end of session,
            # not a transient failure; clear it rather than leaving a
            # dead entry around until its TTL catches up.
            try:
                await session_store.delete(r, session_id)
            except RedisError:
                pass
            request.session.pop(SESSION_KEY, None)
            raise RequireLoginRedirect()

        user["access_token"] = refreshed["access_token"]
        user["access_expires_at"] = time.time() + refreshed.get("expires_in", 300)
        if refreshed.get("refresh_token"):
            user["refresh_token"] = refreshed["refresh_token"]
        if "refresh_expires_in" in refreshed:
            user["refresh_expires_at"] = time.time() + refreshed["refresh_expires_in"]
        try:
            await session_store.set(r, session_id, user)
        except RedisError:
            # The refreshed tokens are good for this request even if we
            # couldn't persist them -- worst case, the next request
            # refreshes again. Don't fail a request that just succeeded.
            pass

    return user


def require_session_role(role: str):
    """FastAPI dependency factory: require_session_role("operator")

    Gates page/screen selection, not actions -- see this module's
    docstring for why this stays role-based rather than moving to
    permission checks like require_permission() below.
    """

    async def checker(request: Request) -> dict:
        user = await get_session_user(request)
        if user["role"] != role:
            raise HTTPException(status_code=403, detail=f"requires role: {role}")
        return user

    return checker


async def check_permission(user: dict, permission: str) -> None:
    """Raise HTTPException if `user` doesn't have `permission`.

    Callable directly from inside a route body (for routes that need to
    pick the required permission based on the request body -- see
    manager_decision in ui.py, which needs Approve or Reject depending
    on the submitted decision, mirroring api/routes.py's
    submit_decision), or via the require_permission()
    dependency factory below for the common single-fixed-permission
    case. Same shape as api/auth.py's check_permission() -- both wrap
    the same workflow/keycloak_auth.get_permissions().
    """
    try:
        granted = await keycloak_auth.get_permissions(user["access_token"])
    except keycloak_auth.TokenInvalid:
        # Session's access token itself is no longer valid (expired
        # between get_session_user()'s own access_expires_at check/refresh
        # and this call, or rejected for some other reason) -- send back through
        # login rather than a bare 401, consistent with how every other
        # session-auth failure in this module behaves.
        raise RequireLoginRedirect()
    except keycloak_auth.PermissionCheckError as e:
        raise HTTPException(status_code=503, detail=f"permission check failed: {e}")
    if permission not in granted:
        raise HTTPException(
            status_code=403, detail=f"requires permission: {permission}"
        )


def require_permission(permission: str):
    """FastAPI dependency factory: require_permission("Create")

    Gates the five mutating actions via a real UMA ticket exchange --
    see this module's docstring for how this differs from
    require_session_role() above.
    """

    async def checker(request: Request) -> dict:
        user = await get_session_user(request)
        await check_permission(user, permission)
        return user

    return checker
