"""
Integration tests for POST /reviews/search (workflow/service.py's
list_reviews_page(), Phase 1 of docs/PAGINATION_PLAN.md), against the
real local stack (Keycloak, Postgres, Temporal, worker) -- creates real
review requests via the existing POST /reviews route, then exercises
pagination, filtering, and the query_id count-cache against them.

Marked `integration` (see pyproject.toml's markers); deselect with
`pytest -m "not integration"` when the stack isn't up. Follows
test_api_permissions.py's conventions (_get_token() duplicated rather
than shared via conftest.py, module-scoped tokens/client fixtures).
"""

import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from review_approval.app import app
from review_approval.workflow import service

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "password"


def _get_token(username: str) -> str:
    issuer = os.environ["KEYCLOAK_ISSUER"]
    response = httpx.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "client_id": os.environ["KEYCLOAK_CLIENT_ID"],
            "client_secret": os.environ["KEYCLOAK_CLIENT_SECRET"],
            "grant_type": "password",
            "username": username,
            "password": DEMO_PASSWORD,
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture(scope="module")
def token():
    return _get_token("operator1")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(client, token, vendor):
    response = client.post(
        "/reviews",
        headers=_auth(token),
        json={
            "review_type": "purchase_order",
            "payload": {"vendor": vendor, "amount": 1, "currency": "USD", "line_items": []},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["request_id"]


@pytest.fixture(scope="module")
def seeded(client, token):
    """5 fresh requests, own vendor tag, so pagination/count assertions in
    this module aren't affected by rows other test modules/runs left
    behind in the same shared Postgres instance."""
    vendor = f"pagination-{uuid.uuid4().hex[:8]}"
    ids = [_create(client, token, vendor) for _ in range(5)]
    return {"vendor": vendor, "ids": ids}


def _search(client, token, **body):
    return client.post("/reviews/search", headers=_auth(token), json=body)


# ---------------------------------------------------------- pagination -----

def test_pagination_pages_through_without_overlap_or_gaps(client, token, seeded):
    first = _search(
        client, token, page=0, page_size=2,
        filter={"requester": "operator1", "review_type": "purchase_order"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["page_size"] == 2
    assert len(first_body["items"]) == 2
    assert first_body["total"] >= 5

    second = _search(client, token, page=1, page_size=2, query_id=first_body["query_id"])
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total"] == first_body["total"]  # served from cache, not recomputed

    first_ids = {item["id"] for item in first_body["items"]}
    second_ids = {item["id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_query_id_reuse_skips_recount(client, token, seeded, monkeypatch):
    call_count = {"n": 0}
    real_count = service._count_reviews

    async def counting_count(*args, **kwargs):
        call_count["n"] += 1
        return await real_count(*args, **kwargs)

    monkeypatch.setattr(service, "_count_reviews", counting_count)

    first = _search(
        client, token,
        filter={"requester": "operator1", "review_type": "purchase_order"},
    )
    assert first.status_code == 200
    assert call_count["n"] == 1

    second = _search(client, token, query_id=first.json()["query_id"], page=1)
    assert second.status_code == 200
    assert call_count["n"] == 1  # cache hit -- no second COUNT(*)


# ------------------------------------------------------------- filtering -----

def test_filter_by_requester_and_review_type_narrows_results(client, token, seeded):
    response = _search(
        client, token,
        filter={"requester": "operator1", "review_type": "purchase_order"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filter"] == {"requester": "operator1", "review_type": "purchase_order"}
    assert all(item["requester"] == "operator1" for item in body["items"])
    assert all(item["review_type"] == "purchase_order" for item in body["items"])


# ------------------------------------------------------------- validation ----

def test_unknown_review_type_rejected(client, token):
    response = _search(client, token, filter={"review_type": "not_a_real_type"})
    assert response.status_code == 400


def test_unknown_query_id_rejected(client, token):
    response = _search(client, token, query_id=str(uuid.uuid4()))
    assert response.status_code == 400


def test_negative_page_rejected(client, token):
    response = _search(client, token, page=-1)
    assert response.status_code == 400
