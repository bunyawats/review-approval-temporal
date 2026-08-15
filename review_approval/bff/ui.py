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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_approval.bff.keycloak_session import (
    build_authorize_url,
    check_permission,
    complete_login,
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


# ------------------------------------------------------------- operator ----

@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request, user: dict = Depends(require_session_role("operator"))):
    _, pool = _clients(request)
    paged = await service.list_reviews_page(pool, page_size=_UI_PAGE_SIZE, filter={"requester": user["username"]})
    permissions = await _user_permissions(user)
    return _render(request, "operator.html", {"user": user, "paged": paged, "permissions": permissions})


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
    _, pool = _clients(request)
    page = max(page, 0)  # Prev is disabled at page 0; clamp against tampering
    paged = None
    if query_id:
        try:
            candidate = await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE, query_id=query_id)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.filter.get("requester") == user["username"]:
            paged = candidate
    if paged is None:
        paged = await service.list_reviews_page(
            pool, page=page, page_size=_UI_PAGE_SIZE, filter={"requester": user["username"]}
        )
    permissions = await _user_permissions(user)
    return _render(request, "_operator_list.html", {"paged": paged, "permissions": permissions})


@router.get("/operator/new-form", response_class=HTMLResponse)
async def new_form(request: Request, user: dict = Depends(require_permission("Create_Request"))):
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
    user: dict = Depends(require_permission("Create_Request")),
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
    return _render(request, "_operator_list.html", {"paged": paged, "permissions": permissions, "clear_dialog": True})


@router.get("/operator/{request_id}/edit-form", response_class=HTMLResponse)
async def edit_form(request: Request, request_id: str, user: dict = Depends(require_permission("Update_Request"))):
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
    user: dict = Depends(require_permission("Update_Request")),
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


@router.get("/operator/{request_id}/cancel-form", response_class=HTMLResponse)
async def cancel_form(request: Request, request_id: str, user: dict = Depends(require_permission("Cancel_Request"))):
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None or record["requester"] != user["username"]:
        raise HTTPException(status_code=404)
    return _render(
        request,
        "_cancel_dialog.html",
        {"request_id": request_id, "review_type": record["review_type"]},
    )


@router.post("/operator/{request_id}/cancel", response_class=HTMLResponse)
async def cancel_request_route(
    request: Request,
    request_id: str,
    comment: str = Form(""),
    user: dict = Depends(require_permission("Cancel_Request")),
):
    client, pool = _clients(request)
    try:
        await service.cancel_review(client, pool, request_id, user["username"], comment)
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


@router.get("/operator/{request_id}/detail", response_class=HTMLResponse)
async def operator_detail(request: Request, request_id: str, user: dict = Depends(require_session_role("operator"))):
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None or record["requester"] != user["username"]:
        raise HTTPException(status_code=404)
    return _render(request, "_detail_dialog.html", {"record": record, "role": "operator"})


# -------------------------------------------------------------- manager ----

@router.get("/manager", response_class=HTMLResponse)
async def manager_page(request: Request, user: dict = Depends(require_session_role("manager"))):
    _, pool = _clients(request)
    paged = await service.list_reviews_page(pool, page_size=_UI_PAGE_SIZE)
    permissions = await _user_permissions(user)
    return _render(request, "manager.html", {"user": user, "paged": paged, "permissions": permissions})


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
    _, pool = _clients(request)
    page = max(page, 0)  # Prev is disabled at page 0; clamp against tampering
    try:
        paged = await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE, query_id=query_id or None)
    except ValueError:
        paged = await service.list_reviews_page(pool, page=page, page_size=_UI_PAGE_SIZE)
    permissions = await _user_permissions(user)
    return _render(request, "_manager_list.html", {"paged": paged, "permissions": permissions})


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
    user: dict = Depends(require_session_role("manager")),
):
    # Approve/reject need different permissions -- which one depends on
    # the submitted decision, so this can't be expressed as a single
    # Depends(require_permission(...)); check it explicitly instead.
    # Mirrors api/routes.py's submit_decision.
    permission = "Approve_Request" if decision == "APPROVED" else "Reject_Request"
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
