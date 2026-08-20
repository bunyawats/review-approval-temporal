"""
Activities: the only code in this project allowed to touch the outside
world (Postgres). Workflows stay pure and never call these directly with
side effects other than through workflow.execute_activity.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import asyncpg
from temporalio import activity

_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"], min_size=1, max_size=5
        )
    return _pool


@dataclass
class PersistRequestInput:
    request_id: str
    review_type: str
    payload: dict[str, Any]
    requester: str
    workflow_id: str


@dataclass
class PersistDecisionInput:
    request_id: str
    decision: str  # APPROVED | REJECTED | CANCELLED
    closed_by: str
    closed_comment: str
    # Only ever set for a native Temporal cancellation (see workflows.py's
    # wait_condition except clause) -- lets closed_at reflect the moment
    # Temporal delivered the cancel rather than whenever this activity
    # happens to run. Unset (None) for every normal signal-driven decision
    # (including a regular CANCELLED), which has no separate "decided at"
    # moment to reconcile against and just uses now() below.
    closed_at: Optional[datetime] = None


@activity.defn
async def persist_request(inp: PersistRequestInput) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO review_requests
                (id, workflow_id, review_type, payload, requester, status)
            VALUES ($1, $2, $3, $4::jsonb, $5, 'PENDING_REVIEW')
            ON CONFLICT (id) DO NOTHING
            """,
            inp.request_id,
            inp.workflow_id,
            inp.review_type,
            json.dumps(inp.payload),
            inp.requester,
        )


@activity.defn
async def persist_decision(inp: PersistDecisionInput) -> None:
    # One activity for all three terminal outcomes (APPROVED, REJECTED,
    # CANCELLED) -- previously persist_decision (Approve/Reject) and
    # persist_cancel were separate activities, but they wrote the same
    # columns with the same shape (persist_cancel just hardcoded
    # status='CANCELLED' where this always took decision as a variable);
    # merged per docs/MERGE_CANCEL_DECISION_PLAN.md.
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE review_requests
            SET status = $2,
                closed_by = $3,
                closed_comment = $4,
                closed_at = COALESCE($5, now())
            WHERE id = $1
            """,
            inp.request_id,
            inp.decision,
            inp.closed_by,
            inp.closed_comment,
            inp.closed_at,
        )


@dataclass
class PersistUpdateInput:
    request_id: str
    payload: dict[str, Any]


@activity.defn
async def persist_update(inp: PersistUpdateInput) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE review_requests
            SET payload = $2::jsonb
            WHERE id = $1 AND status = 'PENDING_REVIEW'
            """,
            inp.request_id,
            json.dumps(inp.payload),
        )
