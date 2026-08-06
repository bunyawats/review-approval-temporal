"""
Core business logic. Neither front door (api/routes.py's JSON API,
bff/ui.py's HTMX pages) talks to Temporal or Postgres directly -- only
this module does. That's what keeps the two front doors (real auth vs.
mock auth) from drifting out of sync on what's actually allowed.
"""

import uuid
from typing import Any, Optional

import asyncpg
from temporalio.client import Client

from review_approval.workflow.schemas import validate_payload
from review_approval.workflow.task_queues import task_queue_for_review_type
from review_approval.workflow.workflows import ReviewApprovalWorkflow, ReviewRequestInput


def workflow_id(request_id: str) -> str:
    return f"review-{request_id}"


async def create_review(
    client: Client,
    pool: asyncpg.Pool,
    review_type: str,
    payload: dict[str, Any],
    requester: str,
) -> str:
    validated = validate_payload(review_type, payload)
    request_id = str(uuid.uuid4())
    await client.start_workflow(
        ReviewApprovalWorkflow.run,
        ReviewRequestInput(
            request_id=request_id,
            review_type=review_type,
            payload=validated,
            requester=requester,
        ),
        id=workflow_id(request_id),
        task_queue=task_queue_for_review_type(review_type),
    )
    return request_id


async def list_reviews(
    pool: asyncpg.Pool, requester: Optional[str] = None
) -> list[dict]:
    async with pool.acquire() as conn:
        if requester:
            rows = await conn.fetch(
                "SELECT * FROM review_requests WHERE requester = $1 "
                "ORDER BY created_at DESC",
                requester,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM review_requests ORDER BY created_at DESC"
            )
    return [dict(r) for r in rows]


async def get_review(pool: asyncpg.Pool, request_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM review_requests WHERE id = $1", request_id
        )
    return dict(row) if row else None


async def update_review(
    client: Client,
    pool: asyncpg.Pool,
    request_id: str,
    requester: str,
    new_payload: dict[str, Any],
) -> None:
    record = await get_review(pool, request_id)
    if record is None:
        raise LookupError("review not found")
    if record["requester"] != requester:
        raise PermissionError("only the requester who created this review can edit it")
    if record["status"] != "PENDING_REVIEW":
        raise ValueError("this review is no longer editable")
    validated = validate_payload(record["review_type"], new_payload)
    handle = client.get_workflow_handle(workflow_id(request_id))
    await handle.signal(ReviewApprovalWorkflow.update_payload, validated)


async def cancel_review(
    client: Client,
    pool: asyncpg.Pool,
    request_id: str,
    requester: str,
    comment: str = "",
) -> None:
    record = await get_review(pool, request_id)
    if record is None:
        raise LookupError("review not found")
    if record["requester"] != requester:
        raise PermissionError(
            "only the requester who created this review can cancel it"
        )
    if record["status"] != "PENDING_REVIEW":
        raise ValueError("this review is no longer cancellable")
    handle = client.get_workflow_handle(workflow_id(request_id))
    await handle.signal(
        ReviewApprovalWorkflow.cancel_request, args=[requester, comment]
    )


async def submit_decision(
    client: Client,
    pool: asyncpg.Pool,
    request_id: str,
    decision: str,
    closed_by: str,
    comment: str,
) -> None:
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("decision must be APPROVED or REJECTED")
    record = await get_review(pool, request_id)
    if record is None:
        raise LookupError("review not found")
    if record["status"] != "PENDING_REVIEW":
        raise ValueError("this review has already been decided")
    handle = client.get_workflow_handle(workflow_id(request_id))
    await handle.signal(
        ReviewApprovalWorkflow.submit_decision, args=[decision, closed_by, comment]
    )
