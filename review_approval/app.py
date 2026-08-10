"""
The FastAPI app itself: assembles both front doors into one process.

  - bff/ui.py's /ui/* routes: real Keycloak session auth
    (keycloak_session.py, Authorization Code flow) -- see
    keycloak/INTEGRATION_PLAN.md for what's still Phase 2/3 in progress.
  - api/routes.py's JSON routes: real Keycloak JWT auth (api/auth.py),
    for actual clients/integrations.
  - bff/sandbox.py's /sandbox/* routes: standalone htmx experiments, no
    auth, no Temporal/Postgres.

Both bff/ui.py and api/routes.py call the same workflow/service.py
functions, so business rules (ownership checks, status checks, payload
validation) live in one place rather than being duplicated per front
door.

Run from anywhere on the Python path (project root, or installed as a
package):

    uvicorn review_approval.app:app --reload --port 8000
"""

import json
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from temporalio.client import Client

from review_approval.api.routes import router as api_router
from review_approval.bff.keycloak_session import RequireLoginRedirect
from review_approval.bff.sandbox import router as sandbox_router
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

# Session cookie for the /ui/* HTMX UI (real Keycloak login, see
# bff/keycloak_session.py). Use a real, stable secret via env var in
# anything beyond a local demo.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("UI_SESSION_SECRET", "dev-only-insecure-secret"),
)


@app.exception_handler(RequireLoginRedirect)
async def _redirect_to_login(request, exc):
    return RedirectResponse(url="/ui/login", status_code=303)


app.include_router(ui_router)
app.include_router(sandbox_router)
app.include_router(api_router)


@app.get("/", tags=["Web UI"])
async def root():
    return RedirectResponse(url="/ui/login")
