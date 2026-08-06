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

from review_approval.api.auth import get_current_user, require_role
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


@router.post("/reviews", status_code=201)
async def create_review(
    request: Request,
    body: CreateReviewRequest,
    user: dict = Depends(require_role("operator")),
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
    user: dict = Depends(require_role("operator")),
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
    user: dict = Depends(require_role("operator")),
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
    user: dict = Depends(require_role("manager")),
):
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


@router.get("/reviews")
async def list_reviews(request: Request, user: dict = Depends(get_current_user)):
    return await service.list_reviews(request.app.state.pg_pool)
