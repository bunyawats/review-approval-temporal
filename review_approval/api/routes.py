"""
JSON REST API: real Keycloak JWT auth (auth.py), for actual
clients/integrations -- as opposed to bff/ui.py's /ui/* routes, which
use mock session auth for the POC demo UI only.

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
    decision: str  # APPROVED | REJECTED
    comment: str = ""


class CancelRequest(BaseModel):
    comment: str = ""


class ReviewSearchFilter(BaseModel):
    requester: Optional[str] = None
    review_type: Optional[str] = None


class ReviewSearchRequest(BaseModel):
    page: Optional[int] = None
    page_size: Optional[int] = None
    query_id: Optional[str] = None
    filter: Optional[ReviewSearchFilter] = None


class BulkCancelRequest(BaseModel):
    request_ids: list[str]
    comment: str = ""


class BulkDecisionRequest(BaseModel):
    request_ids: list[str]
    decision: str  # APPROVED | REJECTED
    comment: str = ""


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
# matches path routes in registration order, and "/reviews/bulk/cancel"
# has the same 3-segment shape as "/reviews/{request_id}/cancel" -- if the
# parameterized route were registered first, it would swallow this one with
# request_id="bulk". Same reasoning applies to bulk/decision vs.
# {request_id}/decision.
@router.post("/reviews/bulk/cancel")
async def bulk_cancel_reviews(
    body: BulkCancelRequest,
    request: Request,
    user: dict = Depends(require_permission("Cancel")),
):
    try:
        results = await service.bulk_cancel_reviews(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            body.request_ids,
            user["sub"],
            body.comment,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _bulk_response(results)


@router.post("/reviews/bulk/decision")
async def bulk_submit_decision(
    body: BulkDecisionRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Approve/reject need different permissions -- which one depends on the
    # request body, same reasoning as the single-item decision route below.
    permission = "Approve" if body.decision == "APPROVED" else "Reject"
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


@router.post("/reviews/{request_id}/cancel")
async def cancel_review(
    request_id: str,
    request: Request,
    user: dict = Depends(require_permission("Cancel")),
    body: Optional[CancelRequest] = None,
):
    comment = body.comment if body else ""
    try:
        await service.cancel_review(
            request.app.state.temporal_client,
            request.app.state.pg_pool,
            request_id,
            user["sub"],
            comment,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="review not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "cancel_sent"}


@router.post("/reviews/{request_id}/decision")
async def submit_decision(
    request_id: str,
    body: DecisionRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # Approve/reject need different permissions -- which one depends on
    # the request body, so this can't be expressed as a single
    # Depends(require_permission(...)); check it explicitly instead.
    permission = "Approve" if body.decision == "APPROVED" else "Reject"
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
