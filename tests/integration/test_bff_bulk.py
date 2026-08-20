"""
Integration tests for bff/ui.py's bulk cancel/approve/reject selection UI
and confirm/execute flow (docs/BULK_ACTIONS_PLAN.md). Operator's routes
are named bulk-decision-form/bulk-decision, not bulk-cancel-*, since
cancelling was merged into decision (see
docs/MERGE_CANCEL_DECISION_PLAN.md) -- decision is hardcoded CANCELLED
server-side on the operator side, so it's never sent in these requests.
Against a REAL local Keycloak instance (docker compose up -d keycloak)
and the real app (native Postgres/Temporal/worker, per README's local
dev setup).

Marked `integration` (see pyproject.toml's markers) since it needs the
local stack up; deselect with `pytest -m "not integration"` when it
isn't. Login mechanics and the sequential-TestClient discipline are
duplicated from test_bff_permissions.py -- see that file's module
docstring for why two TestClients for the same `app` singleton must
never be open at once.
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
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        _login_via_keycloak(client, username)
        yield client


def _create_as(client: TestClient, vendor: str = "test", review_type: str = "purchase_order") -> str:
    # review_type is configurable (not just vendor) because
    # _bulk_confirm_dialog.html's preview deliberately doesn't render raw
    # request ids (see docs/BULK_ACTIONS_PLAN.md's dialog spec: "Lists each
    # item: type, (requester, manager only), current status") -- tests that
    # need to tell selected items apart in that dialog's rendered HTML use
    # review_type as the distinguishing signal instead of id/vendor.
    if review_type == "purchase_order":
        payload_json = f'{{"vendor": "{vendor}", "amount": 1, "currency": "USD", "line_items": []}}'
    else:
        payload_json = f'{{"employee": "{vendor}", "start_date": "2026-01-01", "end_date": "2026-01-02", "reason": "test"}}'
    response = client.post(
        "/ui/operator/requests",
        data={"review_type": review_type, "payload_json": payload_json},
    )
    assert response.status_code == 200, response.text
    match = re.search(r'id="row-([\w-]+)"', response.text)
    assert match, response.text[:500]
    return match.group(1)


def _select(client: TestClient, role: str, request_ids: list[str], checked: bool = True) -> httpx.Response:
    # A single comma-joined string, not a repeated `request_ids=a&
    # request_ids=b` field per id -- matching what a real browser's htmx
    # actually sends via hx-vals (see bff/ui.py's operator_bulk_select()
    # comment for the confirmed FormData.set() coercion behind this).
    # httpx's own list-value encoding (data={"request_ids": [...]})
    # produces the repeated-field shape instead, which does NOT match
    # the real wire format and would silently test the wrong thing.
    response = client.post(
        f"/ui/{role}/bulk-select",
        data={"request_ids": ",".join(request_ids), "checked": "true" if checked else "false"},
    )
    assert response.status_code == 200, response.text
    return response


def _row_checked(html: str, request_id: str) -> bool:
    # Distinguishes an actual `checked` HTML attribute on this row's own
    # checkbox from the literal string "checked" that always appears as a
    # JSON *key name* inside every checkbox's hx-vals, regardless of its
    # real state.
    match = re.search(rf'name="request_ids" value="{re.escape(request_id)}"\s*\n\s*(checked)?', html)
    assert match, f"checkbox for {request_id} not found in:\n{html[:800]}"
    return match.group(1) == "checked"


def _confirm_dialog_count(dialog_text: str) -> int:
    # "Bulk <Action> (N selected)" header -- see _bulk_confirm_dialog.html.
    # Deliberately not asserting on raw request ids in this dialog's body:
    # it only ever renders type/(requester)/status, never the id itself
    # (see docs/BULK_ACTIONS_PLAN.md's dialog spec), so tests that need to
    # tell selected items apart use review_type as the distinguishing
    # signal (see _create_as()) and this header count for "how many".
    match = re.search(r"\((\d+) selected\)", dialog_text)
    assert match, dialog_text[:500]
    return int(match.group(1))


# --------------------------------------------------------------- selection ----

def test_bulk_select_only_mutates_own_selection():
    # Two logged-in operators selecting concurrently never see or clear
    # each other's selections -- _bulk_selection is keyed by username and
    # bulk_select() only ever touches the caller's own entry.
    with _client_as("operator1") as op1:
        op1.get("/ui/operator")  # fresh load: clear any state left by other tests
        id1 = _create_as(op1, vendor="op1-item")
        _select(op1, "operator", [id1])
        dialog = op1.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert _confirm_dialog_count(dialog.text) == 1

    with _client_as("operator2") as op2:
        # operator2 never selected anything -- if operator1's selection had
        # somehow leaked across the shared _bulk_selection dict, this would
        # show a nonzero count.
        op2.get("/ui/operator")
        leaked = op2.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert _confirm_dialog_count(leaked.text) == 0

        id2 = _create_as(op2, vendor="op2-item")
        _select(op2, "operator", [id2])
        dialog2 = op2.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        # operator2's selection is its own, and operator1's earlier
        # selection wasn't cleared by operator2's actions.
        assert _confirm_dialog_count(dialog2.text) == 1

    with _client_as("operator1") as op1_again:
        dialog_again = op1_again.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert _confirm_dialog_count(dialog_again.text) == 1


def test_unchecking_removes_from_selection():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        request_id = _create_as(client)
        _select(client, "operator", [request_id], checked=True)
        _select(client, "operator", [request_id], checked=False)
        dialog = client.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert "Nothing selected" in dialog.text


def test_fresh_page_load_clears_selection_without_confirming():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        request_id = _create_as(client)
        _select(client, "operator", [request_id])
        dialog = client.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert _confirm_dialog_count(dialog.text) == 1

        # A plain GET (not the poll route) clears selection even without
        # ever confirming a bulk action.
        client.get("/ui/operator")
        dialog2 = client.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert "Nothing selected" in dialog2.text


def test_bulk_cancel_form_drops_ids_not_owned_by_this_operator():
    # Defense in depth for the visibility invariant: even if
    # _bulk_selection[username] somehow held another operator's request
    # id, the confirm dialog's preview silently drops it. bulk_select()
    # never validates ownership itself (the plan's design explicitly
    # leaves that to this filter) -- reproduce that "somehow" directly by
    # POSTing another operator's real id into this session's selection.
    # foreign_id/own_id use different review_types so the surviving item
    # can be identified by its rendered type, not by id (see _create_as).
    with _client_as("operator1") as op1:
        foreign_id = _create_as(op1, vendor="not-yours", review_type="purchase_order")

    with _client_as("operator2") as op2:
        op2.get("/ui/operator")
        own_id = _create_as(op2, vendor="yours", review_type="leave_request")
        _select(op2, "operator", [foreign_id, own_id])
        dialog = op2.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        # foreign_id was dropped -- only own_id (a leave_request) survives.
        assert _confirm_dialog_count(dialog.text) == 1
        assert "leave_request" in dialog.text
        assert "purchase_order" not in dialog.text


# --------------------------------------------------------- select-all/toolbar ----
# See docs/SELECT_ALL_CHECKBOX_PLAN.md -- the table is split into three
# independently-refreshable regions (header row w/ select-all checkbox,
# toolbar fragment, tbody). These tests cover the mechanics that split
# enables: the periodic-poll route only ever returns tbody rows, the
# select-all checkbox's own bulk-select request updates every row on the
# page plus the toolbar (out-of-band), and Prev/Next keeps the toolbar's
# baked-in page/query_id in sync even though the toolbar itself lives
# outside #request-list.

def test_periodic_poll_route_returns_table_wrapped_tbody_only():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        request_id = _create_as(client, vendor="poll-target")
        response = client.post("/ui/operator/rows", data={"page": 0, "query_id": ""})
        assert response.status_code == 200, response.text
        assert response.text.strip().startswith("<table>")
        assert 'id="request-rows"' in response.text
        assert f'id="row-{request_id}"' in response.text
        # The poll route never touches selection/toolbar -- its response
        # has no reason to carry a toolbar fragment at all.
        assert "bulk-toolbar-operator" not in response.text


def test_select_all_checks_every_row_and_updates_toolbar_out_of_band():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        ids = [_create_as(client, vendor=f"selall-{i}") for i in range(3)]

        checked = _select(client, "operator", ids, checked=True)
        for request_id in ids:
            assert _row_checked(checked.text, request_id)
        assert "3 selected" in checked.text
        assert 'id="bulk-toolbar-operator"' in checked.text
        assert 'hx-swap-oob="true"' in checked.text

        unchecked = _select(client, "operator", ids, checked=False)
        for request_id in ids:
            assert not _row_checked(unchecked.text, request_id)
        assert "0 selected" in unchecked.text


def test_select_all_checkbox_itself_is_always_rendered_unchecked():
    # Stateless action trigger, not a status indicator -- even when every
    # row on the page is already selected, the header checkbox itself
    # never renders a `checked` attribute (see docs/SELECT_ALL_CHECKBOX_PLAN.md's
    # "Decisions").
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        ids = [_create_as(client, vendor=f"stateless-{i}") for i in range(2)]
        _select(client, "operator", ids, checked=True)

        page = client.get("/ui/operator")
        select_all = re.search(r'<input type="checkbox"\s*\n\s*hx-post="/ui/operator/bulk-select"[^>]*>', page.text)
        assert select_all, page.text[:800]
        assert "checked" not in select_all.group(0).split("hx-post")[0]


def test_per_row_checkbox_click_still_updates_toolbar_out_of_band():
    # Per-row checkboxes keep hx-swap="none" client-side (already visually
    # correct the instant they're clicked), but the response still needs
    # to carry a correct toolbar OOB update -- verified here server-side,
    # since a real browser's handling of the "none" + OOB combination
    # can't be exercised through TestClient.
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        request_id = _create_as(client)
        response = _select(client, "operator", [request_id], checked=True)
        assert "1 selected" in response.text
        assert 'id="bulk-toolbar-operator"' in response.text
        assert 'hx-swap-oob="true"' in response.text


def test_prev_next_navigation_keeps_toolbar_in_sync():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        nav = client.post("/ui/operator/list", data={"page": 0, "query_id": ""})
        assert nav.status_code == 200
        assert 'id="request-list"' in nav.text
        assert 'id="bulk-toolbar-operator"' in nav.text
        assert 'hx-swap-oob="true"' in nav.text


def test_manager_select_all_and_periodic_poll():
    with _client_as("operator1") as client:
        ids = [_create_as(client, vendor=f"mgr-selall-{i}") for i in range(2)]

    with _client_as("manager1") as manager:
        manager.get("/ui/manager")
        poll = manager.post("/ui/manager/rows", data={"page": 0, "query_id": ""})
        assert poll.status_code == 200
        assert poll.text.strip().startswith("<table>")
        assert "bulk-toolbar-manager" not in poll.text

        checked = _select(manager, "manager", ids, checked=True)
        for request_id in ids:
            assert _row_checked(checked.text, request_id)
        assert "2 selected" in checked.text
        assert 'id="bulk-toolbar-manager"' in checked.text


# ----------------------------------------------------------------- cancel ----

def test_bulk_cancel_flow_end_to_end():
    with _client_as("operator1") as client:
        client.get("/ui/operator")
        ids = [_create_as(client, vendor=f"bulk-{i}") for i in range(3)]
        _select(client, "operator", ids)

        dialog = client.post("/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""})
        assert dialog.status_code == 200
        assert _confirm_dialog_count(dialog.text) == 3

        result = client.post(
            "/ui/operator/bulk-decision", data={"comment": "batch cleanup", "page": 0, "query_id": ""}
        )
        assert result.status_code == 200, result.text
        assert "3 succeeded, 0 failed" in result.text
        # Unlike the confirm dialog, the OOB-refreshed table (list_html)
        # does render each row's real id (see _operator_row.html), so this
        # part of the response CAN be checked against the actual ids.
        for request_id in ids:
            assert f'id="row-{request_id}"' in result.text
        assert result.text.count("CANCELLED") >= 3

        # Selection cleared after a confirmed action -- a fresh page load
        # shows nothing checked / "0 selected".
        page = client.get("/ui/operator")
        assert "0 selected" in page.text


def test_manager_cannot_bulk_cancel():
    # Permission enforcement runs before any body logic (require_permission
    # via Depends), so this 403s regardless of whether any request exists.
    with _client_as("manager1") as manager:
        response = manager.post(
            "/ui/operator/bulk-decision-form", data={"page": 0, "query_id": ""}
        )
        assert response.status_code == 403
        assert "Cancel" in response.json()["detail"]


# ---------------------------------------------------------------- decision ----

def test_bulk_approve_flow_end_to_end():
    with _client_as("operator1") as client:
        ids = [_create_as(client, vendor=f"approve-{i}") for i in range(2)]

    with _client_as("manager1") as manager:
        manager.get("/ui/manager")
        _select(manager, "manager", ids)
        dialog = manager.post(
            "/ui/manager/bulk-decision-form",
            data={"decision": "APPROVED", "page": 0, "query_id": ""},
        )
        assert dialog.status_code == 200
        assert _confirm_dialog_count(dialog.text) == 2

        result = manager.post(
            "/ui/manager/bulk-decision",
            data={"decision": "APPROVED", "comment": "lgtm all", "page": 0, "query_id": ""},
        )
        assert result.status_code == 200, result.text
        assert "2 succeeded, 0 failed" in result.text
        for request_id in ids:
            assert f'id="row-{request_id}"' in result.text
        assert result.text.count("APPROVED") >= 2

        page = manager.get("/ui/manager")
        assert "0 selected" in page.text


def test_operator_cannot_bulk_decide():
    # require_session_role("manager") gates this route before any body
    # logic runs, so this 403s regardless of whether any request exists.
    with _client_as("operator1") as client:
        response = client.post(
            "/ui/manager/bulk-decision-form",
            data={"decision": "APPROVED", "page": 0, "query_id": ""},
        )
        assert response.status_code == 403
        assert "requires role: manager" in response.json()["detail"]


def test_bulk_decision_mixed_eligible_and_terminal_shows_per_item_results():
    with _client_as("operator1") as client:
        eligible = _create_as(client, vendor="eligible")
        already_terminal = _create_as(client, vendor="already-terminal")
        cancel_resp = client.post(
            f"/ui/operator/{already_terminal}/decision", data={"comment": "beat you to it"}
        )
        assert cancel_resp.status_code == 200

    with _client_as("manager1") as manager:
        manager.get("/ui/manager")
        _select(manager, "manager", [eligible, already_terminal])
        result = manager.post(
            "/ui/manager/bulk-decision",
            data={"decision": "REJECTED", "comment": "batch", "page": 0, "query_id": ""},
        )
        assert result.status_code == 200
        assert "1 succeeded, 1 failed" in result.text
        assert "already been decided" in result.text
