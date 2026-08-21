"""
Thin Redis wrapper for the /ui/* server-side session store (see
docs/SESSION_STORE_PLAN.md). The browser cookie holds only an opaque
session id (see keycloak_session.py); everything else -- username, role,
and both Keycloak token pairs -- lives here.

Key shape: "ui-session:<session id>" -> JSON-encoded {"username", "role",
"access_token", "access_expires_at", "refresh_token",
"refresh_expires_at"}.

TTL is sliding, not a fixed cap from login: every successful get() pushes
the key's expiry back out to SESSION_TTL_SECONDS, so "session lasts 30
minutes" tracks 30 minutes of *inactivity* -- matching this realm's
ssoSessionIdleTimeout (confirmed live against the running Keycloak
instance, see docs/SESSION_STORE_PLAN.md's "Context" section).

SESSION_TTL_SECONDS itself is defined in workflow/memory_service.py, not
here, even though this module is its "real" meaning (the auth session's
idle timeout) -- workflow/ must never import from bff/, so the shared
constant has to live at that lower layer for this module to import
upward from, same direction every other workflow/->bff/ dependency
already runs. See that module's docstring for the full reasoning.
"""

import json
import secrets

import redis.asyncio as redis

from review_approval.workflow.memory_service import SESSION_TTL_SECONDS

_KEY_PREFIX = "ui-session:"


def new_session_id() -> str:
    # Same generation style as keycloak_session.py's OAuth CSRF state.
    return secrets.token_urlsafe(32)


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


async def get(r: redis.Redis, session_id: str) -> dict | None:
    raw = await r.get(_key(session_id))
    if raw is None:
        return None
    await r.expire(_key(session_id), SESSION_TTL_SECONDS)
    return json.loads(raw)


async def set(r: redis.Redis, session_id: str, data: dict) -> None:
    await r.set(_key(session_id), json.dumps(data), ex=SESSION_TTL_SECONDS)


async def delete(r: redis.Redis, session_id: str) -> None:
    await r.delete(_key(session_id))
