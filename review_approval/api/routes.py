"""
JSON REST API: real Keycloak JWT auth (auth.py), for actual
clients/integrations -- as opposed to bff/ui.py's /ui/* routes, which
use a real Keycloak session (Authorization Code flow, see
bff/keycloak_session.py) for the server-rendered HTMX UI.

Both front doors call the same workflow/service.py functions, so
business rules (ownership checks, status checks, payload validation)
live in one place rather than being duplicated per front door.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from review_approval.api.auth import check_permission, get_current_user, require_permission
from review_approval.workflow import service

router = APIRouter(tags=["REST Services"])


class CreateReviewRequest(BaseModel):
    review_type: str
    payload: dict


class DecisionRequest(BaseModel):
    decision: str  # APPROVED | REJECTED | CANCELLED
    comment: str = ""


class ReviewSearchFilter(BaseModel):
    requester: Optional[str] = None
    review_type: Optional[str] = None


class ReviewSearchRequest(BaseModel):
    page: Optional[int] = None
    page_size: Optional[int] = None
    query_id: Optional[str] = None
    filter: Optional[ReviewSearchFilter] = None


class BulkDecisionRequest(BaseModel):
    request_ids: list[str]
    decision: str  # APPROVED | REJECTED | CANCELLED
    comment: str = ""


# Cancelling is just another decision (see docs/MERGE_CANCEL_DECISION_PLAN.md
# -- this used to be a separate POST /reviews/{id}/cancel + CancelRequest
# body, with its own Cancel-permission-only route). One lookup, shared by
# both the single-item and bulk decision routes below, so the mapping is
# never defined twice. Keycloak itself is unchanged by this merge -- Create/
# Update/Cancel/Approve/Reject stay five independent Scopes on the single
# RequestApproval Resource; this dict just picks which one applies.
_DECISION_PERMISSIONS = {"APPROVED": "Approve", "REJECTED": "Reject", "CANCELLED": "Cancel"}


@router.post("/reviews", status_code=201)
async def create_review(
    request: Request,
    body: CreateReviewRequest,
    user: dict = Depends(require_permission("Create")),
):
    try:
        request_id = await service.create_review(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            body.review_type,
            body.payload,
            user["sub"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"request_id": request_id, "status": "PENDING_REVIEW"}


def _bulk_response(results: list[service.BulkActionResult]) -> dict:
    return {
        "results": [
            {"request_id": r.request_id, "ok": r.ok, "error": r.error} for r in results
        ],
        "succeeded": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
    }


# Registered before the /reviews/{request_id}/... routes below: FastAPI
# matches path routes in registration order, and "/reviews/bulk/decision"
# has the same 3-segment shape as "/reviews/{request_id}/decision" -- if the
# parameterized route were registered first, it would swallow this one with
# request_id="bulk".
@router.post("/reviews/bulk/decision")
async def bulk_submit_decision(
    body: BulkDecisionRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Which permission depends on the request body (Cancel/Approve/Reject),
    # so this can't be expressed as a single Depends(require_permission(...));
    # check it explicitly instead. An unrecognized decision value skips the
    # permission check entirely (there's no scope for "garbage") and falls
    # straight through to service.bulk_submit_decision()'s own ValueError,
    # mapped to 400 below -- never silently defaults to some other
    # permission.
    permission = _DECISION_PERMISSIONS.get(body.decision)
    if permission is not None:
        await check_permission(user, permission)
    try:
        results = await service.bulk_submit_decision(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            body.request_ids,
            body.decision,
            user["sub"],
            body.comment,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _bulk_response(results)


@router.get("/reviews/{request_id}")
async def get_review(request_id: str, request: Request, user: dict = Depends(get_current_user)):
    record = await service.get_review(request.app.state.pg_pool, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="review not found")
    return record


@router.patch("/reviews/{request_id}")
async def update_review(
    request_id: str,
    body: CreateReviewRequest,
    request: Request,
    user: dict = Depends(require_permission("Update")),
):
    try:
        await service.update_review(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            request_id,
            user["sub"],
            body.payload,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="review not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "update_sent"}


@router.post("/reviews/{request_id}/decision")
async def submit_decision(
    request_id: str,
    body: DecisionRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Same permission-by-decision-value pattern as the bulk route above --
    # see _DECISION_PERMISSIONS' comment.
    permission = _DECISION_PERMISSIONS.get(body.decision)
    if permission is not None:
        await check_permission(user, permission)
    try:
        await service.submit_decision(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            request_id,
            body.decision,
            user["sub"],
            body.comment,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="review not found")
    except PermissionError as e:
        # Only reachable for decision="CANCELLED" (wrong-requester attempt)
        # -- Approve/Reject never raise this. Easy to forget when merging
        # cancel into this route: omitting this handler would turn a
        # wrong-requester cancel attempt into an unhandled 500.
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "signal_sent"}


@router.post("/reviews/search")
async def search_reviews(
    body: ReviewSearchRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    filter_dict = body.filter.model_dump() if body.filter else None
    try:
        result = await service.list_reviews_page(
            request.app.state.pg_pool,
            page=body.page,
            page_size=body.page_size,
            query_id=body.query_id,
            filter=filter_dict,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "query_id": result.query_id,
        "page": result.page,
        "page_size": result.page_size,
        "filter": result.filter,
        "total": result.total,
        "items": result.items,
    }
