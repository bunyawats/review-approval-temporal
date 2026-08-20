"""
Integration tests for api/routes.py's bulk endpoint (POST
/reviews/bulk/decision -- POST /reviews/bulk/cancel was removed, see
docs/MERGE_CANCEL_DECISION_PLAN.md), against a REAL local Keycloak
instance (docker compose up -d keycloak) and the real app (native
Postgres/Temporal/worker, per README's local dev setup).

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
# Cancelling is just another decision now -- POST /reviews/bulk/cancel was
# removed (see docs/MERGE_CANCEL_DECISION_PLAN.md); every test below moved
# to POST /reviews/bulk/decision with decision="CANCELLED".

def test_operator_can_bulk_cancel_own_requests(client, tokens):
    ids = [_create_as(client, tokens["operator1"]) for _ in range(3)]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": ids, "decision": "CANCELLED", "comment": "batch cleanup"},
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
        f"/reviews/{already_terminal}/decision",
        headers=_auth(tokens["operator1"]),
        json={"decision": "CANCELLED", "comment": "first"},
    )
    assert cancel_resp.status_code == 200

    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["operator1"]),
        json={"request_ids": [eligible, already_terminal], "decision": "CANCELLED", "comment": "batch"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    by_id = {r["request_id"]: r for r in body["results"]}
    assert by_id[eligible]["ok"]
    assert not by_id[already_terminal]["ok"]
    assert "already been decided" in by_id[already_terminal]["error"]


def test_manager_cannot_bulk_cancel(client, tokens):
    # New case, not reachable before the merge -- decision used to be
    # Approve/Reject-only on this endpoint (see
    # docs/MERGE_CANCEL_DECISION_PLAN.md's permission-branch matrix).
    ids = [_create_as(client, tokens["operator1"])]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["manager1"]),
        json={"request_ids": ids, "decision": "CANCELLED"},
    )
    assert response.status_code == 403
    assert "Cancel" in response.json()["detail"]


def test_operator_cannot_bulk_cancel_someone_elses_request(client, tokens):
    # Ownership, not permission -- a per-item PermissionError surfaces as
    # a per-item failure in the batch, not a whole-batch exception.
    request_id = _create_as(client, tokens["operator1"])
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["operator2"]),
        json={"request_ids": [request_id], "decision": "CANCELLED"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["failed"] == 1
    assert "requester" in body["results"][0]["error"]


def test_bulk_cancel_empty_request_ids_rejected(client, tokens):
    response = client.post(
        "/reviews/bulk/decision", headers=_auth(tokens["operator1"]), json={"request_ids": [], "decision": "CANCELLED"}
    )
    assert response.status_code == 400
    assert "must not be empty" in response.json()["detail"]


def test_bulk_cancel_over_cap_rejected(client, tokens):
    ids = [f"not-real-{i}" for i in range(51)]
    response = client.post(
        "/reviews/bulk/decision", headers=_auth(tokens["operator1"]), json={"request_ids": ids, "decision": "CANCELLED"}
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
    # An unrecognized decision has no corresponding permission scope, so
    # the route skips check_permission() entirely and falls straight
    # through to service.bulk_submit_decision()'s own validation --
    # confirmed by using a manager token here (Approve/Reject granted,
    # Cancel not) despite the invalid value not being any of the three.
    ids = [_create_as(client, tokens["operator1"])]
    response = client.post(
        "/reviews/bulk/decision",
        headers=_auth(tokens["manager1"]),
        json={"request_ids": ids, "decision": "MAYBE"},
    )
    assert response.status_code == 400
    assert "decision must be one of" in response.json()["detail"]
