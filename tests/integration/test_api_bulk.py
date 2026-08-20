"""
Integration tests for api/routes.py's bulk endpoints (POST
/reviews/bulk/cancel, POST /reviews/bulk/decision), against a REAL local
Keycloak instance (docker compose up -d keycloak) and the real app
(native Postgres/Temporal/worker, per README's local dev setup).

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't. Mirrors tests/integration/test_api_permissions.py's fixtures.
"""

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from review_approval.app import app

pytestmark = pytest.mark.integration

DEMO_USERS = ["operator1", "operator2", "manager1", "manager2"]
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
def tokens():
    return {username: _get_token(username) for username in DEMO_USERS}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _purchase_order_payload(vendor="test"):
    return {
        "review_type": "purchase_order",
        "payload": {"vendor": vendor, "amount": 1, "currency": "USD", "line_items": []},
    }


def _create_as(client, token, vendor="test") -> str:
    response = client.post("/reviews", headers=_auth(token), json=_purchase_order_payload(vendor))
    assert response.status_code == 201, response.text
    return response.json()["request_id"]


# -------------------------------------------------------------- bulk cancel ----

def test_operator_can_bulk_cancel_own_requests(client, tokens):
    ids = [_create_as(client, tokens["operator1"]) for _ in range(3)]
    response = client.post(
        "/reviews/bulk/cancel",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": ids, "comment": "batch cleanup"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["succeeded"] == 3
    assert body["failed"] == 0
    assert {r["request_id"] for r in body["results"]} == set(ids)
    assert all(r["ok"] and r["error"] is None for r in body["results"])

    for request_id in ids:
        record = client.get(f"/reviews/{request_id}", headers=_auth(tokens["operator1"])).json()
        assert record["status"] == "CANCELLED"
        assert record["closed_by"]  # JWT "sub" claim, not decoded here -- just check it's set
        assert record["closed_comment"] == "batch cleanup"


def test_bulk_cancel_mixed_eligible_and_terminal_ids_separates_results(client, tokens):
    eligible = _create_as(client, tokens["operator1"])
    already_terminal = _create_as(client, tokens["operator1"])
    cancel_resp = client.post(
        f"/reviews/{already_terminal}/cancel", headers=_auth(tokens["operator1"]), json={"comment": "first"}
    )
    assert cancel_resp.status_code == 200

    response = client.post(
        "/reviews/bulk/cancel",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": [eligible, already_terminal], "comment": "batch"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    by_id = {r["request_id"]: r for r in body["results"]}
    assert by_id[eligible]["ok"]
    assert not by_id[already_terminal]["ok"]
    assert "no longer cancellable" in by_id[already_terminal]["error"]


def test_manager_cannot_bulk_cancel(client, tokens):
    ids = [_create_as(client, tokens["operator1"])]
    response = client.post(
        "/reviews/bulk/cancel", headers=_auth(tokens["manager1"]), json={"request_ids": ids}
    )
    assert response.status_code == 403
    assert "Cancel" in response.json()["detail"]


def test_bulk_cancel_empty_request_ids_rejected(client, tokens):
    response = client.post(
        "/reviews/bulk/cancel", headers=_auth(tokens["operator1"]), json={"request_ids": []}
    )
    assert response.status_code == 400
    assert "must not be empty" in response.json()["detail"]


def test_bulk_cancel_over_cap_rejected(client, tokens):
    ids = [f"not-real-{i}" for i in range(51)]
    response = client.post(
        "/reviews/bulk/cancel", headers=_auth(tokens["operator1"]), json={"request_ids": ids}
    )
    assert response.status_code == 400
    assert "at most 50 requests" in response.json()["detail"]


# ------------------------------------------------------------ bulk decision ----

def test_manager_can_bulk_approve(client, tokens):
    ids = [_create_as(client, tokens["operator1"]) for _ in range(2)]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["manager1"]),
        json={"request_ids": ids, "decision": "APPROVED", "comment": "lgtm all"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    for request_id in ids:
        record = client.get(f"/reviews/{request_id}", headers=_auth(tokens["manager1"])).json()
        assert record["status"] == "APPROVED"
        assert record["closed_comment"] == "lgtm all"


def test_manager_can_bulk_reject(client, tokens):
    ids = [_create_as(client, tokens["operator1"])]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["manager2"]),
        json={"request_ids": ids, "decision": "REJECTED", "comment": "no"},
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == 1


def test_operator_cannot_bulk_approve_or_reject(client, tokens):
    ids = [_create_as(client, tokens["operator1"])]

    approve = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": ids, "decision": "APPROVED"},
    )
    assert approve.status_code == 403
    assert "Approve" in approve.json()["detail"]

    reject = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": ids, "decision": "REJECTED"},
    )
    assert reject.status_code == 403
    assert "Reject" in reject.json()["detail"]


def test_bulk_decision_invalid_decision_value_rejected(client, tokens):
    ids = [_create_as(client, tokens["operator1"])]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["manager1"]),
        json={"request_ids": ids, "decision": "MAYBE"},
    )
    assert response.status_code == 400
    assert "APPROVED or REJECTED" in response.json()["detail"]
