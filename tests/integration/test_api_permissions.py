"""
Integration tests for api/routes.py's permission enforcement, against a
REAL local Keycloak instance (docker compose up -d keycloak) and the
real app (native Postgres/Temporal/worker, per README's local dev
setup) -- the automated version of the manual curl verification done by
hand while building this feature.

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't. Uses FastAPI's TestClient so the app's real lifespan (Temporal
client connect, Postgres pool) runs -- this is not mocking anything
below the HTTP layer.
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


# ------------------------------------------------------------------- create ----

def test_operator_can_create(client, tokens):
    response = client.post("/reviews", headers=_auth(tokens["operator1"]), json=_purchase_order_payload())
    assert response.status_code == 201


def test_manager_cannot_create(client, tokens):
    response = client.post("/reviews", headers=_auth(tokens["manager1"]), json=_purchase_order_payload())
    assert response.status_code == 403
    assert "Create_Request" in response.json()["detail"]


# ------------------------------------------------------------- update/cancel ----

def test_manager_cannot_update(client, tokens):
    request_id = _create_as(client, tokens["operator1"])
    response = client.patch(
        f"/reviews/{request_id}", headers=_auth(tokens["manager1"]), json=_purchase_order_payload("changed")
    )
    assert response.status_code == 403
    assert "Update_Request" in response.json()["detail"]


def test_operator_can_cancel_own_pending_request(client, tokens):
    request_id = _create_as(client, tokens["operator2"])
    response = client.post(
        f"/reviews/{request_id}/cancel", headers=_auth(tokens["operator2"]), json={"comment": "changed my mind"}
    )
    assert response.status_code == 200


def test_manager_cannot_cancel(client, tokens):
    request_id = _create_as(client, tokens["operator1"])
    response = client.post(
        f"/reviews/{request_id}/cancel", headers=_auth(tokens["manager1"]), json={"comment": "not my job"}
    )
    assert response.status_code == 403
    assert "Cancel_Request" in response.json()["detail"]


# ------------------------------------------------------------------ decision ----

def test_operator_cannot_approve_or_reject(client, tokens):
    request_id = _create_as(client, tokens["operator1"])

    approve = client.post(
        f"/reviews/{request_id}/decision", headers=_auth(tokens["operator1"]), json={"decision": "APPROVED"}
    )
    assert approve.status_code == 403
    assert "Approve_Request" in approve.json()["detail"]

    reject = client.post(
        f"/reviews/{request_id}/decision", headers=_auth(tokens["operator1"]), json={"decision": "REJECTED"}
    )
    assert reject.status_code == 403
    assert "Reject_Request" in reject.json()["detail"]


def test_manager_can_approve(client, tokens):
    request_id = _create_as(client, tokens["operator1"])
    response = client.post(
        f"/reviews/{request_id}/decision",
        headers=_auth(tokens["manager1"]),
        json={"decision": "APPROVED", "comment": "lgtm"},
    )
    assert response.status_code == 200


def test_manager_can_reject(client, tokens):
    request_id = _create_as(client, tokens["operator1"])
    response = client.post(
        f"/reviews/{request_id}/decision",
        headers=_auth(tokens["manager2"]),
        json={"decision": "REJECTED", "comment": "no"},
    )
    assert response.status_code == 200


# ------------------------------------------------------------- read routes -----

def test_get_and_search_require_only_authentication_not_permission(client, tokens):
    request_id = _create_as(client, tokens["operator1"])

    # Both roles can read -- get_review/search_reviews are identity-only,
    # no permission gate, per api/routes.py's existing design.
    for token in (tokens["operator1"], tokens["manager1"]):
        response = client.get(f"/reviews/{request_id}", headers=_auth(token))
        assert response.status_code == 200

    response = client.post("/reviews/search", headers=_auth(tokens["manager1"]), json={})
    assert response.status_code == 200


# ----------------------------------------------------------- auth failures -----

def test_no_token_rejected(client):
    response = client.post("/reviews", json=_purchase_order_payload())
    # HTTPBearer's own default for a missing Authorization header, not
    # our permission logic (which never runs in this case) -- confirmed
    # empirically as 401, not 403, against this project's actual
    # fastapi/starlette versions.
    assert response.status_code == 401


def test_garbage_token_rejected(client):
    response = client.post("/reviews", headers=_auth("this.is.garbage"), json=_purchase_order_payload())
    assert response.status_code == 401
