"""
BFF: the only thing outside consumers talk to.

Two front doors into the same service.py logic:
  - This module's JSON routes: real Keycloak JWT auth (auth.py), for
    actual clients/integrations.
  - /ui/* routes (ui.py): mock session auth (mock_auth.py), for the POC
    demo UI only. See ui.py and mock_auth.py docstrings.

Run from anywhere on the Python path (project root, or installed as a
package):

    uvicorn review_approval.bff.main:app --reload --port 8000
"""

import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from temporalio.client import Client

from review_approval.bff import service
from review_approval.bff.auth import get_current_user, require_role
from review_approval.bff.mock_auth import RequireLoginRedirect
from review_approval.bff.ui import router as ui_router


async def _register_jsonb_codec(conn: asyncpg.Connection) -> None:
    # asyncpg returns json/jsonb columns as raw text by default -- without
    # this, review_requests.payload comes back as a JSON *string* everywhere
    # it's read (list_reviews, get_review), not a dict.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    app.state.pg_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"], init=_register_jsonb_codec
    )
    yield
    await app.state.pg_pool.close()


app = FastAPI(title="Review/Approval BFF", lifespan=lifespan)

# POC-only session cookie for the mock-auth UI. Use a real, stable secret
# via env var in anything beyond a local demo.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("UI_SESSION_SECRET", "dev-only-insecure-secret"),
)


@app.exception_handler(RequireLoginRedirect)
async def _redirect_to_login(request, exc):
    return RedirectResponse(url="/ui/login", status_code=303)


app.include_router(ui_router)


@app.get("/", tags=["Web UI"])
async def root():
    return RedirectResponse(url="/ui/login")


class CreateReviewRequest(BaseModel):
    review_type: str
    payload: dict


class DecisionRequest(BaseModel):
    decision: str  # APPROVED | REJECTED
    comment: str = ""


@app.post("/reviews", status_code=201, tags=["REST Services"])
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


@app.get("/reviews/{request_id}", tags=["REST Services"])
async def get_review(request_id: str, request: Request, user: dict = Depends(get_current_user)):
    record = await service.get_review(request.app.state.pg_pool, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="review not found")
    return record


@app.patch("/reviews/{request_id}", tags=["REST Services"])
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


class CancelRequest(BaseModel):
    comment: str = ""


@app.post("/reviews/{request_id}/cancel", tags=["REST Services"])
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


@app.post("/reviews/{request_id}/decision", tags=["REST Services"])
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


@app.get("/reviews", tags=["REST Services"])
async def list_reviews(request: Request, user: dict = Depends(get_current_user)):
    return await service.list_reviews(request.app.state.pg_pool)
