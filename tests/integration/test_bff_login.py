"""
Integration test for bff/keycloak_session.py's real Authorization Code
login flow, against the real local Keycloak instance -- no browser
needed. Keycloak's own hosted login form is POSTed to directly with
httpx, the same technique used interactively while building this
feature (see the keycloak-admin skill's "Authorization Code flow"
section for the gotchas that surfaced doing this by hand: scope=openid
needed for an id_token, exact redirect_uri matching, etc.).

Marked `integration` -- needs docker compose up -d keycloak plus the
native/Dockerized app stack (Postgres/Temporal not actually required
for login itself, but the app's lifespan connects to both on startup).
"""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from review_approval.app import app

pytestmark = pytest.mark.integration


def _extract_form_action(html: str, form_id: str) -> str:
    match = re.search(rf'<form id="{form_id}"[^>]*action="([^"]*)"', html)
    assert match, f"could not find form#{form_id} in Keycloak's response:\n{html[:500]}"
    return match.group(1).replace("&amp;", "&")


def _login_via_keycloak(app_client: TestClient, username: str, password: str) -> httpx.Response:
    """Drives the full Authorization Code flow through our app's
    /ui/login and Keycloak's own hosted login page, ending on whatever
    response /ui/callback returns.

    Uses a fresh, throwaway httpx.Client for the Keycloak-side leg so
    Keycloak's own SSO session cookie never leaks between calls -- two
    logins as different users in the same test run must each see
    Keycloak's real credentials form, not an auto-approved existing SSO
    session.

    Cookies between the GET (authorize) and POST (submit credentials)
    are forwarded *manually* via an explicit Cookie header, not left to
    httpx's automatic cookie-jar tracking -- confirmed by hand that the
    jar (built on Python's stdlib http.cookiejar) mishandles the bare
    hostname "localhost" (no dot), internally scoping cookies to a
    synthetic ".local" domain that doesn't match on the next request,
    so Keycloak's session cookie silently never gets sent and the login
    POST fails with "Cookie not found" -- not a Keycloak problem, an
    httpx/stdlib cookiejar one.
    """
    login_page = app_client.get("/ui/login")
    authorize_url_match = re.search(r'href="([^"]*)"', login_page.text)
    assert authorize_url_match, login_page.text[:500]
    authorize_url = authorize_url_match.group(1).replace("&amp;", "&")

    with httpx.Client() as kc:
        keycloak_login_page = kc.get(authorize_url)
        cookie_header = "; ".join(f"{k}={v}" for k, v in keycloak_login_page.cookies.items())
        form_action = _extract_form_action(keycloak_login_page.text, "kc-form-login")
        keycloak_redirect = kc.post(
            form_action,
            data={"username": username, "password": password, "credentialId": ""},
            headers={"Cookie": cookie_header},
        )
    assert keycloak_redirect.status_code == 302, keycloak_redirect.text
    callback_url = keycloak_redirect.headers["location"]

    return app_client.get(callback_url, follow_redirects=False)


@pytest.fixture
def client():
    # Function-scoped (not module-scoped): each test gets its own clean
    # app-side session cookie jar, so logins/logouts in one test can't
    # leak into another.
    #
    # base_url must match a redirectUri registered on the "review-approval"
    # client (keycloak/import/myrealm-realm.json) -- TestClient's own
    # default ("http://testserver") leaks into keycloak_session.py's
    # request.base_url-derived redirect_uri, which Keycloak then rejects
    # with an opaque error page (still titled "Sign in to <realm>", so it
    # looks like the login form at a glance -- confirmed by actually
    # reading the authorize URL TestClient generated, not assumed).
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def test_operator_login_lands_on_operator_page(client):
    response = _login_via_keycloak(client, "operator1", "password")

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/operator"

    operator_page = client.get("/ui/operator")
    assert operator_page.status_code == 200
    assert "My Review Requests" in operator_page.text


def test_manager_login_lands_on_manager_page(client):
    response = _login_via_keycloak(client, "manager1", "password")

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/manager"

    manager_page = client.get("/ui/manager")
    assert manager_page.status_code == 200


def test_wrong_password_never_reaches_our_callback(client):
    login_page = client.get("/ui/login")
    authorize_url = re.search(r'href="([^"]*)"', login_page.text).group(1).replace("&amp;", "&")

    with httpx.Client() as kc:
        keycloak_login_page = kc.get(authorize_url)
        cookie_header = "; ".join(f"{k}={v}" for k, v in keycloak_login_page.cookies.items())
        form_action = _extract_form_action(keycloak_login_page.text, "kc-form-login")
        response = kc.post(
            form_action,
            data={"username": "operator1", "password": "wrong-password", "credentialId": ""},
            headers={"Cookie": cookie_header},
        )

    # Keycloak re-renders its own login page with an error -- never
    # redirects us a code, so our callback logic never even runs.
    assert response.status_code == 200
    assert "kc-form-login" in response.text


def test_logout_clears_session(client):
    _login_via_keycloak(client, "operator1", "password")
    assert client.get("/ui/operator").status_code == 200

    logout_response = client.post("/ui/logout", follow_redirects=False)
    assert logout_response.status_code == 303
    assert "protocol/openid-connect/logout" in logout_response.headers["location"]

    # Local session is gone regardless of whether the user completes
    # Keycloak's own confirmation step (see the keycloak-admin skill --
    # RP-initiated logout without id_token_hint shows a confirmation
    # page rather than logging out silently; this app doesn't send
    # id_token_hint, by design, to keep the session cookie small).
    denied = client.get("/ui/operator", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/ui/login"
