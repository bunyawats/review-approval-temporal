"""
Integration tests for bff/ui.py's permission enforcement (Phase 3),
against a REAL local Keycloak instance (docker compose up -d keycloak)
and the real app (native Postgres/Temporal/worker, per README's local
dev setup) -- the /ui/* mirror of test_api_permissions.py's REST
permission matrix, but driving real logged-in sessions through
Keycloak's own hosted login form instead of bearer tokens.

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't.

Login mechanics (state-carrying GET, Keycloak's own POST form, manual
Cookie-header forwarding) are duplicated from test_bff_login.py rather
than imported -- this project's existing convention (test_api_permissions.py
duplicates _get_token() from test_keycloak_auth.py rather than sharing a
conftest.py fixture), kept for consistency and because each integration
test file is meant to be readable standalone.

Each test that needs two different logged-in actors (e.g. operator1
creates, manager1 tries to mutate it) opens two TestClients
*sequentially*, one fully closed before the next opens -- NOT two
concurrently-open TestClients. Both wrap the same module-level `app`
singleton, and each `with TestClient(app) as c:` runs the app's real
lifespan (connecting a fresh asyncpg pool bound to that TestClient's
own event loop); with two open at once the second's startup clobbers
`app.state.pg_pool`/`temporal_client` out from under the first, and the
first's shutdown then fails trying to close a pool attached to a
different (already-torn-down) loop -- confirmed by hitting exactly this
`RuntimeError: ... attached to a different loop` when this was first
written with a single fixture handing out multiple simultaneously-open
clients per test.
"""

import re
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from review_approval.app import app

pytestmark = pytest.mark.integration

DEMO_PASSWORD = "password"


def _extract_form_action(html: str, form_id: str) -> str:
    match = re.search(rf'<form id="{form_id}"[^>]*action="([^"]*)"', html)
    assert match, f"could not find form#{form_id} in Keycloak's response:\n{html[:500]}"
    return match.group(1).replace("&amp;", "&")


def _login_via_keycloak(app_client: TestClient, username: str) -> None:
    """Drives the full Authorization Code flow through /ui/login and
    Keycloak's own hosted login page, leaving app_client with a real,
    logged-in session cookie. Cookies between Keycloak's GET and POST
    are forwarded manually via a Cookie header rather than relying on
    httpx's automatic jar, which mishandles the bare hostname
    "localhost" -- see test_bff_login.py's _login_via_keycloak for the
    full explanation of this gotcha.
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


@contextmanager
def _client_as(username: str):
    """A single logged-in TestClient, scoped to a `with` block so its
    lifespan (and the asyncpg pool it opens) is fully torn down before
    any other client for the same `app` singleton is opened -- see this
    module's docstring.

    base_url must match a redirectUri registered on the "review-approval"
    client (keycloak/import/myrealm-realm.json) -- TestClient's own
    default ("http://testserver") leaks into keycloak_session.py's
    request.base_url-derived redirect_uri, which Keycloak then rejects.
    """
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


# ------------------------------------------------------------------- create ----

def test_operator_can_create():
    with _client_as("operator1") as client:
        response = client.get("/ui/operator/new-form")
        assert response.status_code == 200
        _create_as(client)  # asserts internally that the row comes back


def test_manager_cannot_create():
    with _client_as("manager1") as client:
        response = client.post(
            "/ui/operator/requests",
            data={"review_type": "purchase_order", "payload_json": '{"vendor": "x", "amount": 1}'},
        )
        assert response.status_code == 403
        assert "Create_Request" in response.json()["detail"]


# ------------------------------------------------------------- update/cancel ----

def test_manager_cannot_update():
    with _client_as("operator1") as client:
        request_id = _create_as(client)
    with _client_as("manager1") as manager:
        response = manager.post(
            f"/ui/operator/{request_id}/update",
            data={"payload_json": '{"vendor": "changed", "amount": 2}'},
        )
        assert response.status_code == 403
        assert "Update_Request" in response.json()["detail"]


def test_operator_can_cancel_own_pending_request():
    with _client_as("operator2") as client:
        request_id = _create_as(client, vendor="cancel-me")
        response = client.post(f"/ui/operator/{request_id}/cancel", data={"comment": "changed my mind"})
        assert response.status_code == 200
        assert "CANCELLED" in response.text


def test_manager_cannot_cancel():
    with _client_as("operator1") as client:
        request_id = _create_as(client)
    with _client_as("manager1") as manager:
        response = manager.post(f"/ui/operator/{request_id}/cancel", data={"comment": "not my job"})
        assert response.status_code == 403
        assert "Cancel_Request" in response.json()["detail"]


# ------------------------------------------------------------------ decision ----

def test_operator_cannot_reach_manager_decision():
    # manager_decision is gated by require_session_role("manager") first
    # -- an operator session never even reaches the Approve_Request/
    # Reject_Request permission check, unlike the REST API (which has
    # no equivalent role gate on this route, only the permission check).
    with _client_as("operator1") as client:
        request_id = _create_as(client)
        response = client.post(
            f"/ui/manager/{request_id}/decision", data={"decision": "APPROVED", "comment": "self-approve"}
        )
        assert response.status_code == 403
        assert "requires role: manager" in response.json()["detail"]


def test_manager_can_approve():
    with _client_as("operator1") as client:
        request_id = _create_as(client)
    with _client_as("manager1") as manager:
        response = manager.post(
            f"/ui/manager/{request_id}/decision", data={"decision": "APPROVED", "comment": "lgtm"}
        )
        assert response.status_code == 200
        assert "APPROVED" in response.text


def test_manager_can_reject():
    with _client_as("operator1") as client:
        request_id = _create_as(client)
    with _client_as("manager2") as manager:
        response = manager.post(
            f"/ui/manager/{request_id}/decision", data={"decision": "REJECTED", "comment": "no"}
        )
        assert response.status_code == 200
        assert "REJECTED" in response.text


# ----------------------------------------------------------- auth failures -----

def test_no_session_redirects_to_login():
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        response = client.post(
            "/ui/operator/requests",
            data={"review_type": "purchase_order", "payload_json": '{"vendor": "x", "amount": 1}'},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"
