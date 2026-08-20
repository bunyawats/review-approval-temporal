"""
POC HTMX UI. Uses keycloak_session.py (real Keycloak Authorization Code
login) for session auth. Both front doors call the same
workflow/service.py functions, so business rules (ownership checks,
status checks, payload validation) live in one place.

Two authorization dependencies, for two different purposes -- see
keycloak_session.py's module docstring for the full reasoning:
require_session_role() gates page/screen selection (which list a user
sees); require_permission() gates the five mutating actions via a real
Keycloak permission check, same mechanism api/auth.py uses for the REST
API.
"""

import json
import os
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_approval.bff.keycloak_session import (
    build_authorize_url,
    check_permission,
    complete_login,
    get_session_user,
    logout,
    logout_redirect_url,
    require_permission,
    require_session_role,
)
from review_approval.workflow import keycloak_auth, service
from review_approval.workflow.schemas import REVIEW_TYPE_SCHEMAS, SAMPLE_PAYLOADS

router = APIRouter(prefix="/ui", tags=["Web UI"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)

# UI-only display choice, smaller than list_reviews_page()'s own 20-row
# default -- doesn't affect the REST API, which still gets that default
# when it omits page_size. Passed explicitly on every call (including
# query_id-lookup ones): the count cache only stores (filter, total), not
# page_size, so it has to be resupplied every time to keep row slicing
# consistent with what's actually rendered.
_UI_PAGE_SIZE = 10

# username -> set of selected request_ids, for the bulk cancel/approve/reject
# selection UI (see docs/BULK_ACTIONS_PLAN.md's "BFF: selection UI"). In-
# process only, deliberately -- same "correct without a shared cache, a
# replica miss just shows unchecked, never wrong data" reasoning as
# service.py's _query_cache. If/when docs/SESSION_STORE_PLAN.md's Redis
# session store lands, this can move there for free; _get_selection()/
# _clear_selection() stay the seam, only their bodies would change.
_bulk_selection: dict[str, set[str]] = {}


def _get_selection(username: str) -> set[str]:
    return _bulk_selection.setdefault(username, set())


def _clear_selection(username: str) -> None:
    _bulk_selection.pop(username, None)


def _clients(request: Request):
    return request.app.state.temporal_client, request.app.state.pg_pool


async def _user_permissions(user: dict) -> set[str]:
    """The logged-in user's actual granted permissions, for button-
    visibility purposes -- defense in depth alongside the route-level
    require_permission()/check_permission() checks, not a replacement
    for them (a user could always reach a route directly regardless of
    what a template renders). Same UMA ticket exchange as those checks,
    called once per page/row render rather than once per button -- no
    caching, matching the rest of this effort's "no caching in the
    first pass" stance; if this page-load latency ever matters, that's
    the thing to fix, not by skipping this check.
    """
    try:
        return await keycloak_auth.get_permissions(user["access_token"])
    except (keycloak_auth.TokenInvalid, keycloak_auth.PermissionCheckError):
        # Can't confirm what's granted -- fail closed (show no action
        # buttons) rather than crash the page; the route-level checks
        # are still the real guard if this ever masks a genuine outage.
        return set()


def _render(
    request: Request,
    template: str,
    ctx: dict,
    status_code: int = 200,
    headers: dict | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, ctx, status_code=status_code, headers=headers
    )


def _toolbar_oob(role: str, permissions: set[str], selected_ids: set[str], page: int, query_id: str) -> str:
    """Out-of-band render of the bulk-selection toolbar fragment (see
    docs/SELECT_ALL_CHECKBOX_PLAN.md) -- the toolbar lives outside
    #request-list now (a sibling <div>, not a <tr> inside its <thead>),
    specifically so the periodic poll can be scoped to just the table's
    <tbody> without also rebuilding the header row's select-all checkbox
    every cycle. Every route whose response could otherwise leave this
    fragment's baked-in page/query_id or selected-count stale -- page
    navigation (Prev/Next), any checkbox action, a fresh create, a
    completed bulk action -- calls this and appends the result to its
    response. The one exception is the initial full-page load, which
    renders this same template inline (oob=False) via
    operator.html/manager.html instead.
    """
    return templates.get_template(f"_{role}_bulk_toolbar.html").render(
        {
            "permissions": permissions,
            "selected_ids": selected_ids,
            "page": page,
            "query_id": query_id,
            "oob": True,
        }
    )


def _bulk_result_response(
    request: Request,
    action: str,
    results: list[service.BulkActionResult],
    role: str,
    list_ctx: dict,
) -> HTMLResponse:
    """A bulk execute route's response needs three independent fragments
    in one body (see docs/BULK_ACTIONS_PLAN.md's "Results + table
    refresh" and docs/SELECT_ALL_CHECKBOX_PLAN.md): a small results
    dialog swapped into #dialog-container, the caller's list table
    re-rendered with hx-swap-oob="true" so it reflects the new statuses
    (including for selected rows that aren't on the currently-displayed
    page), and the toolbar (now "0 selected" -- the caller already
    cleared selection by this point). These are independent templates,
    so each is rendered to a string via templates.get_template().render()
    and concatenated. `list_ctx` must contain `paged`/`permissions`/
    `selected_ids`.
    """
    dialog_html = templates.get_template("_bulk_result_dialog.html").render(
        {"request": request, "action": action, "results": results}
    )
    list_html = templates.get_template(f"_{role}_list.html").render({"request": request, "oob": True, **list_ctx})
    toolbar_html = _toolbar_oob(
        role, list_ctx["permissions"], list_ctx["selected_ids"], list_ctx["paged"].page, list_ctx["paged"].query_id
    )
    return HTMLResponse(dialog_html + list_html + toolbar_html)


# Error responses from routes whose success path retargets the swap to a
# single row (HX-Retarget below) still need to land in the dialog instead --
# these headers tell htmx to swap the re-rendered dialog fragment into
# #dialog-container regardless of what hx-target the triggering element had.
# HX-Reselect is required too: the triggering action's htmx.ajax() call sets
# select: 'tr' (see the row-swap templates), and without overriding it here
# that filter would still apply to this dialog fragment on error -- which
# has no <tr> in it at all -- silently blanking the dialog instead of
# showing the error. #dialog-root is the dialog's own outer wrapper div,
# present in every _form_dialog.html/_detail_dialog.html response.
_RETARGET_DIALOG_HEADERS = {
    "HX-Retarget": "#dialog-container",
    "HX-Reswap": "innerHTML",
    "HX-Reselect": "#dialog-root",
}


def _parse_payload_or_none(payload_json: str) -> Any:
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- login ----

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render(request, "login.html", {"authorize_url": build_authorize_url(request)})


@router.get("/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return _render(request, "login.html", {"error": f"Keycloak login failed: {error}", "authorize_url": build_authorize_url(request)}, 400)
    if not code or not state:
        return _render(request, "login.html", {"error": "Missing code/state from Keycloak.", "authorize_url": build_authorize_url(request)}, 400)
    try:
        await complete_login(request, code, state)
    except ValueError as e:
        return _render(request, "login.html", {"error": str(e), "authorize_url": build_authorize_url(request)}, 400)
    role = request.session["user"]["role"]
    return RedirectResponse(url=f"/ui/{role}", status_code=303)


@router.post("/logout")
async def logout_submit(request: Request):
    redirect_url = logout_redirect_url(request)
    logout(request)
    return RedirectResponse(url=redirect_url, status_code=303)


async def _resolve_operator_page(pool: asyncpg.Pool, user: dict, page: int, query_id: str) -> service.PagedReviews:
    """Shared by operator_list() and the bulk-decision execute route (both
    need to re-render the same list page a user was already on) -- see
    operator_list()'s own docstring-length comment for why the cached
    filter's requester has to be re-checked against this session before
    it's trusted.
    """
    page = max(page, 0)  # Prev is disabled at page 0; clamp against tampering
    if query_id:
        try:
            candidate = await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE, query_id=query_id)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.filter.get("requester") == user["username"]:
            return candidate
    return await service.list_reviews_page(
        pool, page=page, page_size=_UI_PAGE_SIZE, filter={"requester": user["username"]}
    )


# ------------------------------------------------------------- operator ----

@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request, user: dict = Depends(require_session_role("operator"))):
    # A fresh navigation/reload clears any stale selection from a previous
    # visit -- see docs/BULK_ACTIONS_PLAN.md's "Clearing selection".
    _clear_selection(user["username"])
    _, pool = _clients(request)
    paged = await service.list_reviews_page(pool, page_size=_UI_PAGE_SIZE, filter={"requester": user["username"]})
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    return _render(
        request,
        "operator.html",
        {
            "user": user,
            "paged": paged,
            "permissions": permissions,
            "selected_ids": selected_ids,
            "page": paged.page,
            "query_id": paged.query_id,
        },
    )


@router.post("/operator/list", response_class=HTMLResponse)
async def operator_list(
    request: Request,
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("operator")),
):
    # Paging/polling never resends filter -- it round-trips query_id so
    # the server resolves (filter, total) from the cache, same as the
    # manager list. But unlike manager, the cache here holds someone's
    # *identity* (filter.requester), and query_id is just an opaque
    # string the client hands back -- nothing stops a tampered or
    # cross-session query_id from resolving to a DIFFERENT operator's
    # cached filter. So the cached filter's requester is checked against
    # this session's actual username before it's trusted; any mismatch
    # (including expiry/unknown query_id, same as the manager fallback)
    # re-mints a fresh, correctly-filtered entry instead. This is what
    # actually enforces the operator-visibility invariant now that the
    # filter itself is no longer sent on every request -- see CLAUDE.md's
    # "Operators see only requests where requester == their username".
    #
    # Now used only by Prev/Next (the periodic poll targets #request-rows
    # directly via a separate route -- see operator_rows() below and
    # docs/SELECT_ALL_CHECKBOX_PLAN.md). Prev/Next genuinely changes page,
    # so unlike the poll it also OOB-refreshes the toolbar fragment,
    # which lives outside #request-list and wouldn't otherwise pick up
    # the new page/query_id baked into its Bulk-action button(s).
    _, pool = _clients(request)
    paged = await _resolve_operator_page(pool, user, page, query_id)
    permissions = await _user_permissions(user)
    # Must NOT clear selection, only reflect current state.
    selected_ids = _get_selection(user["username"])
    table_html = templates.get_template("_operator_list.html").render(
        {"request": request, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob("operator", permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(table_html + toolbar_html)


@router.post("/operator/rows", response_class=HTMLResponse)
async def operator_rows(
    request: Request,
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("operator")),
):
    # The periodic 5s self-poll's own target, scoped to just the table
    # body (see docs/SELECT_ALL_CHECKBOX_PLAN.md) -- this is what keeps
    # the header row's select-all checkbox from being destroyed and
    # rebuilt every cycle regardless of whether the user is mid-
    # interaction with it. Same page/query_id resolution as
    # operator_list(); doesn't touch selection or the toolbar, since
    # polling alone never changes either.
    _, pool = _clients(request)
    paged = await _resolve_operator_page(pool, user, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    return _render(
        request,
        "_operator_rows.html",
        {"paged": paged, "permissions": permissions, "selected_ids": selected_ids},
    )


@router.post("/operator/bulk-select", response_class=HTMLResponse)
async def operator_bulk_select(
    request: Request,
    request_ids: str = Form(""),
    checked: bool = Form(...),
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("operator")),
):
    # request_ids arrives as a single comma-joined string, not a repeated
    # form field per id -- htmx's hx-vals passes a JS array value
    # straight to FormData.set(name, value), which (per the FormData/
    # URLSearchParams spec) stringifies any non-string/non-Blob value via
    # Array.prototype.toString() -- i.e. comma-joined into ONE field --
    # rather than appending one field per element. Confirmed against the
    # real pinned htmx4 bundle (htmx.org@4.0.0-beta6): its hx-vals
    # handling does `n.set(e, t[e])`, never `n.append()`, with no
    # array-aware special-casing. The templates now build this string
    # explicitly (`.join(",")` for select-all, a bare id for a per-row
    # checkbox) rather than relying on the coercion implicitly -- see
    # _operator_row.html's/_operator_list.html's matching comments.
    # request_ids never contains a literal comma (ids are UUIDs), so
    # splitting on "," is unambiguous. This mismatch between what htmx
    # actually sends and what this route used to parse
    # (`list[str] = Form([])`, expecting repeated fields) is what broke
    # the select-all checkbox's *value* from this feature's very first
    # version -- a per-row checkbox's single-element array happened to
    # "work" by accident, since a 1-element array's toString() has no
    # comma to begin with.
    ids = [i for i in request_ids.split(",") if i]
    # Gated by role only, not require_permission("Cancel") -- marking a row
    # "selected" has no side effect beyond what's rendered back to this same
    # user, so it doesn't need the heavier permission check. The real
    # enforcement point is cancel_review(), invoked from the execute route.
    sel = _get_selection(user["username"])
    (sel.update if checked else sel.difference_update)(ids)
    # Every bulk-select response carries two independent fragments (see
    # docs/SELECT_ALL_CHECKBOX_PLAN.md): the current page's rows, wrapped
    # for <table> parsing safety -- meaningfully used as the primary swap
    # target only by the select-all checkbox's own request
    # (hx-target="#request-rows" hx-select="tbody" hx-swap="outerHTML");
    # a per-row checkbox ignores it via hx-swap="none" -- and an OOB
    # toolbar update, used by both. One route serves both cases without
    # branching on which checkbox triggered it, same philosophy as the
    # original selection design.
    _, pool = _clients(request)
    paged = await _resolve_operator_page(pool, user, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    rows_html = templates.get_template("_operator_rows.html").render(
        {"request": request, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob("operator", permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(rows_html + toolbar_html)


@router.get("/operator/new-form", response_class=HTMLResponse)
async def new_form(request: Request, user: dict = Depends(require_permission("Create"))):
    return _render(
        request,
        "_form_dialog.html",
        {
            "mode": "create",
            "review_types": list(REVIEW_TYPE_SCHEMAS),
            "sample_payloads": SAMPLE_PAYLOADS,
        },
    )


@router.post("/operator/requests", response_class=HTMLResponse)
async def create_request(
    request: Request,
    review_type: str = Form(...),
    payload_json: str = Form(...),
    user: dict = Depends(require_permission("Create")),
):
    client, pool = _clients(request)
    payload = _parse_payload_or_none(payload_json)
    if payload is None:
        return _render(
            request,
            "_form_dialog.html",
            {
                "mode": "create",
                "review_types": list(REVIEW_TYPE_SCHEMAS),
                "review_type": review_type,
                "payload_json": payload_json,
                "error": "Payload must be valid JSON.",
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    try:
        await service.create_review(client, pool, review_type, payload, user["username"])
    except ValueError as e:
        return _render(
            request,
            "_form_dialog.html",
            {
                "mode": "create",
                "review_types": list(REVIEW_TYPE_SCHEMAS),
                "review_type": review_type,
                "payload_json": payload_json,
                "error": str(e),
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    # New requests sort first (created_at DESC) -- page 0 always shows it.
    paged = await service.list_reviews_page(pool, page_size=_UI_PAGE_SIZE, filter={"requester": user["username"]})
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    list_html = templates.get_template("_operator_list.html").render(
        {
            "request": request,
            "paged": paged,
            "permissions": permissions,
            "selected_ids": selected_ids,
            "clear_dialog": True,
        }
    )
    # The view just reset to page 0 -- refresh the toolbar's baked-in
    # page/query_id to match, same reasoning as operator_list()'s OOB
    # toolbar refresh on Prev/Next.
    toolbar_html = _toolbar_oob("operator", permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(list_html + toolbar_html)


@router.get("/operator/{request_id}/edit-form", response_class=HTMLResponse)
async def edit_form(request: Request, request_id: str, user: dict = Depends(require_permission("Update"))):
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None or record["requester"] != user["username"]:
        raise HTTPException(status_code=404)
    return _render(
        request,
        "_form_dialog.html",
        {
            "mode": "edit",
            "request_id": request_id,
            "review_type": record["review_type"],
            "payload_json": json.dumps(record["payload"], indent=2),
            "review_types": list(REVIEW_TYPE_SCHEMAS),
        },
    )


@router.post("/operator/{request_id}/update", response_class=HTMLResponse)
async def update_request(
    request: Request,
    request_id: str,
    payload_json: str = Form(...),
    user: dict = Depends(require_permission("Update")),
):
    client, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    payload = _parse_payload_or_none(payload_json)
    if payload is None:
        return _render(
            request,
            "_form_dialog.html",
            {
                "mode": "edit",
                "request_id": request_id,
                "review_type": record["review_type"] if record else "",
                "payload_json": payload_json,
                "review_types": list(REVIEW_TYPE_SCHEMAS),
                "error": "Payload must be valid JSON.",
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    try:
        await service.update_review(client, pool, request_id, user["username"], payload)
    except (LookupError, PermissionError, ValueError) as e:
        return _render(
            request,
            "_form_dialog.html",
            {
                "mode": "edit",
                "request_id": request_id,
                "review_type": record["review_type"] if record else "",
                "payload_json": payload_json,
                "review_types": list(REVIEW_TYPE_SCHEMAS),
                "error": str(e),
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    updated = await service.get_review(pool, request_id)
    permissions = await _user_permissions(user)
    return _render(request, "_operator_row_response.html", {"record": updated, "permissions": permissions})


@router.get("/operator/{request_id}/detail", response_class=HTMLResponse)
async def operator_detail(request: Request, request_id: str, user: dict = Depends(require_session_role("operator"))):
    # Also the dialog-opener for Cancel (replaces the old dedicated
    # .../cancel-form route + _cancel_dialog.html -- see
    # docs/MERGE_CANCEL_DECISION_PLAN.md). permissions now has to be
    # computed here, unlike before this merge, since _detail_dialog.html's
    # Cancel branch needs to know whether "Cancel" is actually granted --
    # the manager side already needed this for its Approve/Reject branch.
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None or record["requester"] != user["username"]:
        raise HTTPException(status_code=404)
    permissions = await _user_permissions(user)
    return _render(
        request, "_detail_dialog.html", {"record": record, "role": "operator", "permissions": permissions}
    )


@router.post("/operator/{request_id}/decision", response_class=HTMLResponse)
async def operator_decision(
    request: Request,
    request_id: str,
    comment: str = Form(""),
    user: dict = Depends(require_permission("Cancel")),
):
    # Replaces the old dedicated .../cancel route -- decision is hardcoded
    # here, never read from the request body, since an operator session
    # can never legitimately submit anything but CANCELLED (see
    # docs/MERGE_CANCEL_DECISION_PLAN.md). No 3-way branch needed on this
    # side, only on the REST API's single shared endpoint.
    client, pool = _clients(request)
    try:
        await service.submit_decision(client, pool, request_id, "CANCELLED", user["username"], comment)
    except (LookupError, PermissionError, ValueError):
        pass  # POC: swallow and just show current true state on refresh
    updated = await service.get_review(pool, request_id)
    if updated is None:
        # Genuinely gone (not a normal flow -- nothing in this app deletes
        # rows outright). outerHTML-swapping empty content just removes
        # the row, which is the reasonable outcome here.
        return HTMLResponse("")
    permissions = await _user_permissions(user)
    return _render(request, "_operator_row_response.html", {"record": updated, "permissions": permissions})


@router.post("/operator/bulk-decision-form", response_class=HTMLResponse)
async def operator_bulk_decision_form(
    request: Request,
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_permission("Cancel")),
):
    # Replaces the old .../bulk-cancel-form route, renamed to match
    # manager's bulk-decision-form naming (see
    # docs/MERGE_CANCEL_DECISION_PLAN.md). decision is hardcoded
    # CANCELLED, same reasoning as operator_decision() above.
    _, pool = _clients(request)
    ids = _get_selection(user["username"])
    items = await service.get_reviews(pool, list(ids))
    # Defense in depth for the visibility invariant, same principle as
    # every other operator route (see CLAUDE.md's "Visibility" invariant)
    # -- _bulk_selection[username] should never actually contain another
    # user's id, since bulk_select() only ever mutates the caller's own
    # entry, but this keeps the preview honest regardless.
    items = [r for r in items if r["requester"] == user["username"]]
    return _render(
        request,
        "_bulk_confirm_dialog.html",
        {
            "action": "Cancel",
            "items": items,
            "post_url": "/ui/operator/bulk-decision",
            "role": "operator",
            "decision": None,
            "page": page,
            "query_id": query_id,
        },
    )


@router.post("/operator/bulk-decision", response_class=HTMLResponse)
async def operator_bulk_decision_execute(
    request: Request,
    comment: str = Form(""),
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_permission("Cancel")),
):
    # Replaces the old .../bulk-cancel route -- decision hardcoded
    # CANCELLED, never read from the request body, same reasoning as
    # operator_decision() above.
    client, pool = _clients(request)
    ids = _get_selection(user["username"])
    try:
        results = await service.bulk_submit_decision(client, pool, list(ids), "CANCELLED", user["username"], comment)
    except ValueError as e:
        # Empty selection or over the _MAX_BULK_SIZE cap (the latter only
        # reachable if a selection spans enough pages to exceed it) --
        # re-render the confirm dialog with the error rather than a raw
        # 400, same pattern as every other dialog's error path.
        items = await service.get_reviews(pool, list(ids))
        items = [r for r in items if r["requester"] == user["username"]]
        return _render(
            request,
            "_bulk_confirm_dialog.html",
            {
                "action": "Cancel",
                "items": items,
                "post_url": "/ui/operator/bulk-decision",
                "role": "operator",
                "decision": None,
                "page": page,
                "query_id": query_id,
                "error": str(e),
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    # An id can legitimately go stale between selection and this point
    # (someone else acted on it) -- fine, that's exactly the best-effort,
    # per-item-results semantics; bulk_submit_decision() already reflects it.
    _clear_selection(user["username"])
    paged = await _resolve_operator_page(pool, user, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])  # empty -- just cleared
    return _bulk_result_response(
        request,
        "Cancel",
        results,
        "operator",
        {"paged": paged, "permissions": permissions, "selected_ids": selected_ids},
    )


async def _resolve_manager_page(pool: asyncpg.Pool, page: int, query_id: str) -> service.PagedReviews:
    """Shared by manager_list() and the bulk-decision execute route -- see
    manager_list()'s own comment for the count-cache/fallback reasoning.
    """
    page = max(page, 0)  # Prev is disabled at page 0; clamp against tampering
    try:
        return await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE, query_id=query_id or None)
    except ValueError:
        return await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE)


# -------------------------------------------------------------- manager ----

@router.get("/manager", response_class=HTMLResponse)
async def manager_page(request: Request, user: dict = Depends(require_session_role("manager"))):
    # A fresh navigation/reload clears any stale selection from a previous
    # visit -- see docs/BULK_ACTIONS_PLAN.md's "Clearing selection".
    _clear_selection(user["username"])
    _, pool = _clients(request)
    paged = await service.list_reviews_page(pool, page_size=_UI_PAGE_SIZE)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    return _render(
        request,
        "manager.html",
        {
            "user": user,
            "paged": paged,
            "permissions": permissions,
            "selected_ids": selected_ids,
            "page": paged.page,
            "query_id": paged.query_id,
        },
    )


@router.post("/manager/list", response_class=HTMLResponse)
async def manager_list(
    request: Request,
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("manager")),
):
    # No requester filter (manager visibility is unrestricted), so unlike
    # the operator poll, this one actually benefits from the count cache:
    # a query_id round-tripped from the previous render skips the COUNT(*)
    # entirely. The cache's 30s TTL is shorter than nothing (it never
    # refreshes itself on read), so a query_id that outlives it raises
    # ValueError -- fall back to a fresh, uncached page at the same page
    # number rather than resetting the user to page 0 or erroring out a
    # poll they never directly triggered.
    #
    # Now used only by Prev/Next -- the periodic poll targets #request-rows
    # directly via manager_rows() below (see
    # docs/SELECT_ALL_CHECKBOX_PLAN.md). Prev/Next also OOB-refreshes the
    # toolbar fragment, which lives outside #request-list and wouldn't
    # otherwise pick up the new page/query_id.
    _, pool = _clients(request)
    paged = await _resolve_manager_page(pool, page, query_id)
    permissions = await _user_permissions(user)
    # Must NOT clear selection, only reflect current state.
    selected_ids = _get_selection(user["username"])
    table_html = templates.get_template("_manager_list.html").render(
        {"request": request, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob("manager", permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(table_html + toolbar_html)


@router.post("/manager/rows", response_class=HTMLResponse)
async def manager_rows(
    request: Request,
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("manager")),
):
    # The periodic 5s self-poll's own target -- see operator_rows()'s
    # comment and docs/SELECT_ALL_CHECKBOX_PLAN.md.
    _, pool = _clients(request)
    paged = await _resolve_manager_page(pool, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    return _render(
        request,
        "_manager_rows.html",
        {"paged": paged, "permissions": permissions, "selected_ids": selected_ids},
    )


@router.post("/manager/bulk-select", response_class=HTMLResponse)
async def manager_bulk_select(
    request: Request,
    request_ids: str = Form(""),
    checked: bool = Form(...),
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("manager")),
):
    # See operator_bulk_select()'s comment for why this is a single
    # comma-joined string, not repeated form fields.
    ids = [i for i in request_ids.split(",") if i]
    sel = _get_selection(user["username"])
    (sel.update if checked else sel.difference_update)(ids)
    # See operator_bulk_select()'s comment and
    # docs/SELECT_ALL_CHECKBOX_PLAN.md -- rows fragment (meaningfully
    # used only by the select-all checkbox's own request) + OOB toolbar
    # update (used by both), one route serving both cases.
    _, pool = _clients(request)
    paged = await _resolve_manager_page(pool, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])
    rows_html = templates.get_template("_manager_rows.html").render(
        {"request": request, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob("manager", permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(rows_html + toolbar_html)


@router.post("/manager/bulk-decision-form", response_class=HTMLResponse)
async def bulk_decision_form(
    request: Request,
    decision: str = Form(...),
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(require_session_role("manager")),
):
    # Approve/reject need different permissions -- which one depends on
    # the submitted decision, so this can't be expressed as a single
    # Depends(require_permission(...)); check it explicitly instead.
    # Mirrors manager_decision()/api/routes.py's submit_decision.
    permission = "Approve" if decision == "APPROVED" else "Reject"
    await check_permission(user, permission)
    _, pool = _clients(request)
    ids = _get_selection(user["username"])
    items = await service.get_reviews(pool, list(ids))
    # No visibility filter needed here -- managers see everything.
    return _render(
        request,
        "_bulk_confirm_dialog.html",
        {
            "action": "Approve" if decision == "APPROVED" else "Reject",
            "items": items,
            "post_url": "/ui/manager/bulk-decision",
            "role": "manager",
            "decision": decision,
            "page": page,
            "query_id": query_id,
        },
    )


@router.post("/manager/bulk-decision", response_class=HTMLResponse)
async def bulk_decision_execute(
    request: Request,
    decision: str = Form(...),
    comment: str = Form(""),
    page: int = Form(0),
    query_id: str = Form(""),
    user: dict = Depends(get_session_user),
):
    # No require_session_role("manager") gate here (see
    # docs/MERGE_CANCEL_DECISION_PLAN.md's permission-architecture
    # decision) -- the real Keycloak permission check below is
    # sufficient on its own, same as the REST API relies on with no role
    # gate at all. get_session_user still applies (a route can't be
    # reached with no session at all); it's specifically the role check
    # being dropped, not authentication itself.
    permission = "Approve" if decision == "APPROVED" else "Reject"
    await check_permission(user, permission)
    client, pool = _clients(request)
    ids = _get_selection(user["username"])
    action = "Approve" if decision == "APPROVED" else "Reject"
    try:
        results = await service.bulk_submit_decision(client, pool, list(ids), decision, user["username"], comment)
    except ValueError as e:
        # Empty selection, over the _MAX_BULK_SIZE cap, or (shouldn't
        # happen -- the toolbar only ever submits a value it rendered
        # itself) an invalid decision value -- re-render the confirm
        # dialog with the error, same pattern as every other dialog.
        items = await service.get_reviews(pool, list(ids))
        return _render(
            request,
            "_bulk_confirm_dialog.html",
            {
                "action": action,
                "items": items,
                "post_url": "/ui/manager/bulk-decision",
                "role": "manager",
                "decision": decision,
                "page": page,
                "query_id": query_id,
                "error": str(e),
            },
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    _clear_selection(user["username"])
    paged = await _resolve_manager_page(pool, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = _get_selection(user["username"])  # empty -- just cleared
    return _bulk_result_response(
        request,
        action,
        results,
        "manager",
        {"paged": paged, "permissions": permissions, "selected_ids": selected_ids},
    )


@router.get("/manager/{request_id}/detail", response_class=HTMLResponse)
async def manager_detail(request: Request, request_id: str, user: dict = Depends(require_session_role("manager"))):
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None:
        raise HTTPException(status_code=404)
    permissions = await _user_permissions(user)
    return _render(request, "_detail_dialog.html", {"record": record, "role": "manager", "permissions": permissions})


@router.post("/manager/{request_id}/decision", response_class=HTMLResponse)
async def manager_decision(
    request: Request,
    request_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(get_session_user),
):
    # No require_session_role("manager") gate here (see
    # docs/MERGE_CANCEL_DECISION_PLAN.md's permission-architecture
    # decision) -- the real Keycloak permission check below is
    # sufficient on its own, same as the REST API relies on with no role
    # gate at all, and same as bulk_decision_execute() above.
    # get_session_user still applies; it's specifically the role check
    # being dropped, not authentication itself.
    #
    # Approve/reject need different permissions -- which one depends on
    # the submitted decision, so this can't be expressed as a single
    # Depends(require_permission(...)); check it explicitly instead.
    # Mirrors api/routes.py's submit_decision.
    permission = "Approve" if decision == "APPROVED" else "Reject"
    await check_permission(user, permission)
    client, pool = _clients(request)
    try:
        await service.submit_decision(client, pool, request_id, decision, user["username"], comment)
    except (LookupError, ValueError) as e:
        record = await service.get_review(pool, request_id)
        permissions = await _user_permissions(user)
        return _render(
            request,
            "_detail_dialog.html",
            {"record": record, "role": "manager", "error": str(e), "permissions": permissions},
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    updated = await service.get_review(pool, request_id)
    permissions = await _user_permissions(user)
    return _render(request, "_manager_row_response.html", {"record": updated, "permissions": permissions})
