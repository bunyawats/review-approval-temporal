"""
POC HTMX UI. Uses mock_auth.py (session cookie, no password) instead of
the Keycloak JWT auth that api/auth.py provides for the JSON API. Both
front doors call the same workflow/service.py functions, so business
rules (ownership checks, status checks, payload validation) live in one
place.

NOT for production use as-is -- see mock_auth.py's docstring.
"""

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_approval.bff.mock_auth import login, logout, require_session_role
from review_approval.workflow import service
from review_approval.workflow.schemas import REVIEW_TYPE_SCHEMAS, SAMPLE_PAYLOADS

router = APIRouter(prefix="/ui", tags=["Web UI"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


def _clients(request: Request):
    return request.app.state.temporal_client, request.app.state.pg_pool


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
_RETARGET_DIALOG_HEADERS = {"HX-Retarget": "#dialog-container", "HX-Reswap": "innerHTML"}


def _parse_payload_or_none(payload_json: str) -> Any:
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- login ----

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render(request, "login.html", {})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), role: str = Form(...)):
    try:
        login(request, username, role)
    except ValueError as e:
        return _render(request, "login.html", {"error": str(e), "username": username}, 400)
    return RedirectResponse(url=f"/ui/{role}", status_code=303)


@router.post("/logout")
async def logout_submit(request: Request):
    logout(request)
    return RedirectResponse(url="/ui/login", status_code=303)


# ------------------------------------------------------------- operator ----

@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request, user: dict = Depends(require_session_role("operator"))):
    _, pool = _clients(request)
    reviews = await service.list_reviews(pool, requester=user["username"])
    return _render(request, "operator.html", {"user": user, "reviews": reviews})


@router.get("/operator/list", response_class=HTMLResponse)
async def operator_list(request: Request, user: dict = Depends(require_session_role("operator"))):
    _, pool = _clients(request)
    reviews = await service.list_reviews(pool, requester=user["username"])
    return _render(request, "_operator_list.html", {"reviews": reviews})


@router.get("/operator/new-form", response_class=HTMLResponse)
async def new_form(request: Request, user: dict = Depends(require_session_role("operator"))):
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
    user: dict = Depends(require_session_role("operator")),
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
    reviews = await service.list_reviews(pool, requester=user["username"])
    return _render(request, "_operator_list.html", {"reviews": reviews, "clear_dialog": True})


@router.get("/operator/{request_id}/edit-form", response_class=HTMLResponse)
async def edit_form(request: Request, request_id: str, user: dict = Depends(require_session_role("operator"))):
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
    user: dict = Depends(require_session_role("operator")),
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
    return _render(request, "_operator_row_response.html", {"record": updated, "clear_dialog": True})


@router.post("/operator/{request_id}/cancel", response_class=HTMLResponse)
async def cancel_request_route(
    request: Request,
    request_id: str,
    comment: str = Form(""),
    user: dict = Depends(require_session_role("operator")),
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
    return _render(request, "_operator_row_response.html", {"record": updated})


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
    reviews = await service.list_reviews(pool)
    return _render(request, "manager.html", {"user": user, "reviews": reviews})


@router.get("/manager/list", response_class=HTMLResponse)
async def manager_list(request: Request, user: dict = Depends(require_session_role("manager"))):
    _, pool = _clients(request)
    reviews = await service.list_reviews(pool)
    return _render(request, "_manager_list.html", {"reviews": reviews})


@router.get("/manager/{request_id}/detail", response_class=HTMLResponse)
async def manager_detail(request: Request, request_id: str, user: dict = Depends(require_session_role("manager"))):
    _, pool = _clients(request)
    record = await service.get_review(pool, request_id)
    if record is None:
        raise HTTPException(status_code=404)
    return _render(request, "_detail_dialog.html", {"record": record, "role": "manager"})


@router.post("/manager/{request_id}/decision", response_class=HTMLResponse)
async def manager_decision(
    request: Request,
    request_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(require_session_role("manager")),
):
    client, pool = _clients(request)
    try:
        await service.submit_decision(client, pool, request_id, decision, user["username"], comment)
    except (LookupError, ValueError) as e:
        record = await service.get_review(pool, request_id)
        return _render(
            request,
            "_detail_dialog.html",
            {"record": record, "role": "manager", "error": str(e)},
            400,
            headers=_RETARGET_DIALOG_HEADERS,
        )
    updated = await service.get_review(pool, request_id)
    return _render(request, "_manager_row_response.html", {"record": updated, "clear_dialog": True})
