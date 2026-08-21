"""
Integration tests for the Redis-backed /ui/* session store and token
refresh (docs/SESSION_STORE_PLAN.md's Phase 3) -- against a REAL local
Keycloak instance (docker compose up -d keycloak) and a REAL local Redis
(Homebrew redis-server, or `docker compose up -d redis`; either way,
whatever REDIS_URL points at).

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't.

Login mechanics are duplicated from test_bff_login.py rather than
imported -- this project's existing convention, see that file's
_login_via_keycloak docstring for the httpx-cookiejar gotcha this works
around.

Each test connects its own throwaway redis.asyncio client directly to
REDIS_URL to inspect/mutate the session entry the app itself wrote --
the whole point of these tests is to simulate things happening to that
Redis entry out from under the app (eviction, an already-expired access
token) without waiting on real wall-clock time.
"""

import os
import re

import httpx
import pytest
import redis.asyncio as redis
from fastapi.testclient import TestClient

from review_approval.app import app
from review_approval.bff import session_store

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "password"


def _extract_form_action(html: str, form_id: str) -> str:
    match = re.search(rf'<form id="{form_id}"[^>]*action="([^"]*)"', html)
    assert match, f"could not find form#{form_id} in Keycloak's response:\n{html[:500]}"
    return match.group(1).replace("&amp;", "&")


def _login_via_keycloak(app_client: TestClient, username: str) -> None:
    """Drives the full Authorization Code flow through /ui/login and
    Keycloak's own hosted login page, leaving app_client with a real,
    logged-in session cookie. See test_bff_login.py's _login_via_keycloak
    for the full explanation of the manual Cookie-header-forwarding
    gotcha this works around.
    """
    login_page = app_client.get("/ui/login")
    authorize_url = re.search(r'href="([^"]*)"', login_page.text).group(1).replace("&amp;", "&")

    with httpx.Client() as kc:
        keycloak_login_page = kc.get(authorize_url)
        cookie_header = "; ".join(f"{k}={v}" for k, v in keycloak_login_page.cookies.items())
        form_action = _extract_form_action(keycloak_login_page.text, "kc-form-login")
        keycloak_redirect = kc.post(
            form_action,
            data={"username": username, "password": DEMO_PASSWORD, "credentialId": ""},
            headers={"Cookie": cookie_header},
        )
    assert keycloak_redirect.status_code == 302, keycloak_redirect.text
    callback_response = app_client.get(keycloak_redirect.headers["location"], follow_redirects=False)
    assert callback_response.status_code == 303, callback_response.text


async def _find_new_session_id(r: redis.Redis, keys_before: set[str]) -> str:
    """The session id minted by the login that just happened -- found by
    diffing Redis's key set rather than decoding the app's own signed
    cookie, so these tests don't need to duplicate keycloak_session.py's
    cookie-signing internals just to get at a session id."""
    keys_after = set(await r.keys("ui-session:*"))
    new_keys = keys_after - keys_before
    assert len(new_keys) == 1, f"expected exactly one new session key, got {new_keys}"
    return next(iter(new_keys)).removeprefix("ui-session:")


@pytest.fixture
def client():
    # Function-scoped, matching test_bff_login.py -- each test gets its
    # own clean cookie jar so logins/logouts can't leak between tests.
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


@pytest.fixture
async def redis_client():
    async with redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as r:
        yield r


async def test_missing_session_entry_forces_relogin(client, redis_client):
    keys_before = set(await redis_client.keys("ui-session:*"))
    _login_via_keycloak(client, "operator1")
    assert client.get("/ui/operator").status_code == 200

    session_id = await _find_new_session_id(redis_client, keys_before)
    await session_store.delete(redis_client, session_id)

    # Simulates eviction/restart/expiry -- the cookie is still present
    # and still signed correctly, but Redis has nothing under it anymore.
    response = client.get("/ui/operator", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_refresh_is_transparent_on_expired_access_token(client, redis_client):
    keys_before = set(await redis_client.keys("ui-session:*"))
    _login_via_keycloak(client, "operator1")

    session_id = await _find_new_session_id(redis_client, keys_before)
    data = await session_store.get(redis_client, session_id)
    original_access_token = data["access_token"]

    # Force expiry without waiting 5 real minutes -- the stored
    # refresh_token is untouched and still good, so get_session_user()
    # should transparently exchange it for a new access_token.
    data["access_expires_at"] = 0
    await session_store.set(redis_client, session_id, data)

    response = client.get("/ui/operator", follow_redirects=False)
    assert response.status_code == 200

    refreshed = await session_store.get(redis_client, session_id)
    assert refreshed["access_token"] != original_access_token
    assert refreshed["access_expires_at"] > 0


async def test_logout_clears_redis_entry(client, redis_client):
    keys_before = set(await redis_client.keys("ui-session:*"))
    _login_via_keycloak(client, "operator1")
    assert client.get("/ui/operator").status_code == 200

    session_id = await _find_new_session_id(redis_client, keys_before)
    assert await session_store.get(redis_client, session_id) is not None

    logout_response = client.post("/ui/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    assert await session_store.get(redis_client, session_id) is None


async def test_invalid_refresh_token_falls_back_to_relogin(client, redis_client):
    keys_before = set(await redis_client.keys("ui-session:*"))
    _login_via_keycloak(client, "operator1")

    session_id = await _find_new_session_id(redis_client, keys_before)
    data = await session_store.get(redis_client, session_id)

    # Both tokens broken: access_token expired so get_session_user() must
    # refresh, and refresh_token corrupted so Keycloak rejects the
    # refresh itself -- exercises RefreshFailed's fallback path rather
    # than relying on real 30-minute idle expiry.
    data["access_expires_at"] = 0
    data["refresh_token"] = "not-a-real-refresh-token"
    await session_store.set(redis_client, session_id, data)

    response = client.get("/ui/operator", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"

    # The failed-refresh path also cleans up the now-dead Redis entry
    # rather than leaving it until its TTL catches up.
    assert await session_store.get(redis_client, session_id) is None
