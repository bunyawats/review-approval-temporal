"""
Integration tests for workflow/memory_service.py's Redis I/O, against a
REAL local Redis (same instance the app itself uses -- see REDIS_URL),
plus confirming bff/keycloak_session.py's logout() cleans up the
ui-memory:<id> entry alongside the auth session's ui-session:<id> one
(docs/SESSION_MEMORY_PLAN.md's Phase 1).

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't.
"""

import os
import re

import httpx
import pytest
import redis.asyncio as redis
from fastapi.testclient import TestClient

from review_approval.app import app
from review_approval.bff import session_store
from review_approval.workflow.memory_service import PaginationMemory, SessionMemory

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "password"


@pytest.fixture
async def redis_client():
    async with redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as r:
        yield r


async def test_load_on_unknown_session_id_returns_fresh_empty_memory(redis_client):
    memory = await SessionMemory.load(redis_client, "no-such-session-id")

    assert memory == SessionMemory(pagination=None, bulk_selection=[])


async def test_save_then_load_round_trips_including_pagination(redis_client):
    session_id = "test-memory-service-round-trip"
    memory = SessionMemory(
        pagination=PaginationMemory(
            query_id="q-1", filter={"requester": "operator1", "review_type": None}, total=5, cached_at=1234.5
        ),
        bulk_selection=["req-1", "req-2"],
    )

    try:
        await memory.save(redis_client, session_id)
        loaded = await SessionMemory.load(redis_client, session_id)

        assert loaded == memory
    finally:
        await SessionMemory.delete(redis_client, session_id)


async def test_delete_removes_the_entry(redis_client):
    session_id = "test-memory-service-delete"
    await SessionMemory(bulk_selection=["req-1"]).save(redis_client, session_id)

    await SessionMemory.delete(redis_client, session_id)

    assert await SessionMemory.load(redis_client, session_id) == SessionMemory()


# ---------------------------------------------------------- logout wiring ----


def _extract_form_action(html: str, form_id: str) -> str:
    match = re.search(rf'<form id="{form_id}"[^>]*action="([^"]*)"', html)
    assert match, f"could not find form#{form_id} in Keycloak's response:\n{html[:500]}"
    return match.group(1).replace("&amp;", "&")


def _login_via_keycloak(app_client: TestClient, username: str) -> None:
    """See tests/integration/test_bff_login.py's _login_via_keycloak for
    the full explanation of the manual Cookie-header-forwarding gotcha
    this works around."""
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


@pytest.fixture
def client():
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


async def test_logout_clears_ui_memory_entry_alongside_ui_session(client, redis_client):
    keys_before = set(await redis_client.keys("ui-session:*"))
    _login_via_keycloak(client, "operator1")
    keys_after = set(await redis_client.keys("ui-session:*"))
    new_keys = keys_after - keys_before
    assert len(new_keys) == 1, new_keys
    session_id = next(iter(new_keys)).removeprefix("ui-session:")

    # Nothing has written real pagination/selection data yet (Phase 1 --
    # no call sites wired in), but logout() should still attempt the
    # ui-memory:<id> delete unconditionally and not error even though
    # there's nothing there.
    assert await SessionMemory.load(redis_client, session_id) == SessionMemory()

    logout_response = client.post("/ui/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    assert await session_store.get(redis_client, session_id) is None
    assert await SessionMemory.load(redis_client, session_id) == SessionMemory()
