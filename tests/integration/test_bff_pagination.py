"""
Integration tests for bff/ui.py's pagination wiring (Phase 2 of
docs/PAGINATION_PLAN.md) -- /ui/operator, /ui/operator/list,
/ui/manager, /ui/manager/list, against the real local stack.
Complements test_service_pagination.py (unit, fakes the pool) and
test_api_pagination.py (REST API) by covering what's specific to the
BFF layer: the smaller _UI_PAGE_SIZE, that paging/polling actually
reuses the count cache (skips COUNT(*)) via a round-tripped query_id,
and the requester-match check that stops one operator's session from
riding another operator's cached query_id.

Marked `integration` (see pyproject.toml's markers); deselect with
`pytest -m "not integration"` when the local stack isn't up. Login
helpers duplicated from test_bff_permissions.py rather than imported --
this project's existing convention, kept so each integration test file
reads standalone.
"""

import os
import re
import uuid
from contextlib import contextmanager

import httpx
import pytest
import redis.asyncio as redis
from fastapi.testclient import TestClient

from review_approval.app import app
from review_approval.workflow import memory_service, service

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "password"
UI_PAGE_SIZE = 10  # bff/ui.py's _UI_PAGE_SIZE


def _extract_form_action(html: str, form_id: str) -> str:
    match = re.search(rf'<form id="{form_id}"[^>]*action="([^"]*)"', html)
    assert match, f"could not find form#{form_id} in Keycloak's response:\n{html[:500]}"
    return match.group(1).replace("&amp;", "&")


def _login_via_keycloak(app_client: TestClient, username: str) -> None:
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


@contextmanager
def _client_as(username: str):
    """See test_bff_permissions.py's module docstring for why each
    logged-in client is opened/closed sequentially, never two at once."""
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        _login_via_keycloak(client, username)
        yield client


def _create_as(client: TestClient, vendor: str = "test") -> str:
    response = client.post(
        "/ui/operator/requests",
        data={
            "review_type": "purchase_order",
            "payload_json": f'{{"vendor": "{vendor}", "amount": 1, "currency": "USD", "line_items": []}}',
        },
    )
    assert response.status_code == 200, response.text
    match = re.search(r'id="row-([\w-]+)"', response.text)
    assert match, response.text[:500]
    return match.group(1)


def _query_id(html: str) -> str:
    match = re.search(r'"query_id": "([^"]*)"', html)
    assert match, html[:500]
    return match.group(1)


def _summary(html: str) -> str:
    match = re.search(r"Showing[^<]*|No requests yet\.", html)
    return match.group(0).strip() if match else ""


def _row_count(html: str) -> int:
    return len(re.findall(r'<tr id="row-', html))


@pytest.fixture(scope="module")
def seeded_operator1():
    """UI_PAGE_SIZE + 1 fresh requests as operator1, so pagination has a
    real, guaranteed second page regardless of whatever else is already
    in the dev DB."""
    vendor = f"bff-pagination-{uuid.uuid4().hex[:8]}"
    with _client_as("operator1") as client:
        for _ in range(UI_PAGE_SIZE + 1):
            _create_as(client, vendor=vendor)
    return vendor


# --------------------------------------------------------------- operator ----

def test_operator_page_uses_ui_page_size(seeded_operator1):
    with _client_as("operator1") as client:
        response = client.get("/ui/operator")
        assert response.status_code == 200
        assert _row_count(response.text) == UI_PAGE_SIZE
        assert "1–10 of" in _summary(response.text)


def test_operator_paging_reuses_query_id_and_advances_page(seeded_operator1):
    with _client_as("operator1") as client:
        first = client.get("/ui/operator")
        qid = _query_id(first.text)

        second = client.post("/ui/operator/list", data={"page": "1", "query_id": qid})
        assert second.status_code == 200
        assert _query_id(second.text) == qid  # same cache entry reused
        assert "11–" in _summary(second.text)

        first_ids = set(re.findall(r'<tr id="row-([\w-]+)"', first.text))
        second_ids = set(re.findall(r'<tr id="row-([\w-]+)"', second.text))
        assert first_ids.isdisjoint(second_ids)


def test_operator_query_id_lookup_skips_recount(seeded_operator1, monkeypatch):
    call_count = {"n": 0}
    real_count = service._count_reviews

    async def counting_count(*args, **kwargs):
        call_count["n"] += 1
        return await real_count(*args, **kwargs)

    monkeypatch.setattr(service, "_count_reviews", counting_count)

    with _client_as("operator1") as client:
        first = client.get("/ui/operator")
        assert call_count["n"] == 1
        qid = _query_id(first.text)

        # simulate the 5s self-poll: same page, same query_id
        poll = client.post("/ui/operator/list", data={"page": "0", "query_id": qid})
        assert poll.status_code == 200
        assert call_count["n"] == 1  # no new COUNT(*) -- cache hit


def test_operator_cannot_ride_another_operators_query_id(seeded_operator1):
    with _client_as("operator1") as op1_client:
        op1_page = op1_client.get("/ui/operator")
        op1_qid = _query_id(op1_page.text)

    with _client_as("operator2") as op2_client:
        own_page = op2_client.get("/ui/operator")
        own_total = _summary(own_page.text)

        attack = op2_client.post("/ui/operator/list", data={"page": "0", "query_id": op1_qid})
        assert attack.status_code == 200
        # fell back to a fresh, correctly-filtered entry -- never op1's
        assert _query_id(attack.text) != op1_qid
        assert _summary(attack.text) == own_total


def test_operator_unknown_query_id_falls_back_gracefully(seeded_operator1):
    with _client_as("operator1") as client:
        response = client.post("/ui/operator/list", data={"page": "0", "query_id": str(uuid.uuid4())})
        assert response.status_code == 200
        assert _row_count(response.text) == UI_PAGE_SIZE


def test_operator_pagination_falls_back_to_session_memory_on_cross_replica_miss(seeded_operator1, monkeypatch):
    """Simulates a query_id resolving on a different Kubernetes replica
    than the one that minted it: workflow/service.py's own in-process
    _query_cache is a miss, but this session's Redis memory
    (docs/SESSION_MEMORY_PLAN.md's Phase 3) still has a fresh, matching
    entry and is used instead of a fresh COUNT(*).
    """
    call_count = {"n": 0}
    real_count = service._count_reviews

    async def counting_count(*args, **kwargs):
        call_count["n"] += 1
        return await real_count(*args, **kwargs)

    monkeypatch.setattr(service, "_count_reviews", counting_count)

    with _client_as("operator1") as client:
        # Empty query_id forces a full recompute -- the only path that
        # writes this session's pagination memory (see
        # _remember_pagination()'s "not on every call" design).
        first = client.post("/ui/operator/list", data={"page": "0", "query_id": ""})
        assert first.status_code == 200
        assert call_count["n"] == 1
        qid = _query_id(first.text)
        original_summary = _summary(first.text)

        # Simulate landing on a different replica: this query_id's entry
        # in service.py's own in-process cache is gone, but the session's
        # own Redis memory (written by the call above) is untouched.
        service._query_cache.pop(qid, None)

        second = client.post("/ui/operator/list", data={"page": "0", "query_id": qid})
        assert second.status_code == 200
        assert call_count["n"] == 1  # no new COUNT(*) -- resolved via session memory
        assert _summary(second.text) == original_summary
        assert _query_id(second.text) == qid  # the fallback reuses the same query_id


async def test_operator_pagination_fallback_never_trusts_a_foreign_requester(seeded_operator1):
    """The Redis fallback re-checks filter.requester the same way the
    client-query_id path already does. In real usage a session's own
    memory can never actually hold a foreign requester -- it's only ever
    written by that same session's own recompute, always with its own
    username (see _remember_pagination()) -- so this test manually
    corrupts the stored entry to force the scenario, the same "can't
    really happen, but keep the check honest" defense-in-depth spirit as
    operator_bulk_decision_form()'s own visibility-invariant re-check.
    """
    r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        with _client_as("operator2") as client:
            keys_before = set(await r.keys("ui-memory:*"))
            # Empty query_id forces a full recompute, populating this
            # session's own pagination memory with operator2's real filter.
            first = client.post("/ui/operator/list", data={"page": "0", "query_id": ""})
            own_summary = _summary(first.text)

            keys_after = set(await r.keys("ui-memory:*"))
            new_keys = keys_after - keys_before
            assert len(new_keys) == 1, new_keys
            session_id = next(iter(new_keys)).removeprefix("ui-memory:")

            memory = await memory_service.SessionMemory.load(r, session_id)
            assert memory.pagination.filter.get("requester") == "operator2"
            memory.pagination.filter["requester"] = "operator1"  # corrupt it
            await memory.save(r, session_id)

            # A query_id service.py's own cache has never seen forces the
            # fallback tier to run -- it must refuse the poisoned entry
            # (requester mismatch) and recompute fresh instead of trusting it.
            attack = client.post("/ui/operator/list", data={"page": "0", "query_id": str(uuid.uuid4())})
            assert attack.status_code == 200
            assert _summary(attack.text) == own_summary
    finally:
        await r.aclose()


# ---------------------------------------------------------------- manager ----

def test_manager_page_uses_ui_page_size(seeded_operator1):
    with _client_as("manager1") as client:
        response = client.get("/ui/manager")
        assert response.status_code == 200
        assert _row_count(response.text) == UI_PAGE_SIZE
        assert "1–10 of" in _summary(response.text)


def test_manager_query_id_lookup_skips_recount(seeded_operator1, monkeypatch):
    call_count = {"n": 0}
    real_count = service._count_reviews

    async def counting_count(*args, **kwargs):
        call_count["n"] += 1
        return await real_count(*args, **kwargs)

    monkeypatch.setattr(service, "_count_reviews", counting_count)

    with _client_as("manager1") as client:
        first = client.get("/ui/manager")
        assert call_count["n"] == 1
        qid = _query_id(first.text)

        second = client.post("/ui/manager/list", data={"page": "1", "query_id": qid})
        assert second.status_code == 200
        assert call_count["n"] == 1  # no new COUNT(*) -- cache hit
        assert _query_id(second.text) == qid


def test_manager_unknown_query_id_falls_back_gracefully(seeded_operator1):
    with _client_as("manager1") as client:
        response = client.post("/ui/manager/list", data={"page": "0", "query_id": str(uuid.uuid4())})
        assert response.status_code == 200
        assert _row_count(response.text) == UI_PAGE_SIZE
