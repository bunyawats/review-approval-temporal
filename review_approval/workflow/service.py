"""
Core business logic. Neither front door (api/routes.py's JSON API,
bff/ui.py's HTMX pages) talks to Temporal or Postgres directly -- only
this module does. That's what keeps the two front doors (bearer-token
auth vs. session-cookie auth, both real Keycloak) from drifting out of
sync on what's actually allowed.
"""

import asyncio
import time
import uuid
from typing import Any, Callable, Optional

import asyncpg
from temporalio.client import Client

from review_approval.workflow.schemas import validate_payload
from review_approval.workflow.task_queues import task_queue_for_review_type
from review_approval.workflow.workflows import ReviewApprovalWorkflow, ReviewRequestInput

# client.start_workflow()/handle.signal() only wait for Temporal to accept
# the start/signal -- NOT for the workflow to run its handler or for the
# resulting persist_* activity to actually commit to Postgres. That happens
# asynchronously, whenever a worker process picks up the workflow/activity
# task. Without this, create_review/cancel_review/submit_decision/
# update_review could return before their write lands, so a caller that
# immediately re-queries Postgres (e.g. to re-render a list) can see stale
# data -- on a local dev box this race is usually won by luck (everything's
# fast and on localhost), not by anything the code actually guarantees.
_CONFIRM_TIMEOUT_S = 5.0
_CONFIRM_INTERVAL_S = 0.05


async def _wait_until(
    pool: asyncpg.Pool, request_id: str, predicate: Callable[[dict], bool]
) -> Optional[dict]:
    """Poll Postgres until `predicate(record)` is true or we time out.

    Always returns whatever the last-read record was (or None), even on
    timeout -- callers should never fail a request just because the
    activity is running unusually slowly; they just won't have confirmed
    it landed within the wait budget.
    """
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_S
    while True:
        record = await get_review(pool, request_id)
        if record is not None and predicate(record):
            return record
        if time.monotonic() >= deadline:
            return record
        await asyncio.sleep(_CONFIRM_INTERVAL_S)


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
    # start_workflow only confirms Temporal accepted the start -- wait for
    # the persist_request activity to actually insert the row before
    # returning, so a caller that immediately lists reviews sees it.
    await _wait_until(pool, request_id, lambda record: True)
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
    # signal() only confirms Temporal accepted it -- wait for persist_update
    # to actually write the new payload before returning.
    await _wait_until(pool, request_id, lambda record: record["payload"] == validated)


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
    # signal() only confirms Temporal accepted it -- wait for persist_cancel
    # to actually write status='CANCELLED' before returning.
    await _wait_until(pool, request_id, lambda record: record["status"] == "CANCELLED")


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
    # signal() only confirms Temporal accepted it -- wait for persist_decision
    # to actually write the new status before returning.
    await _wait_until(pool, request_id, lambda record: record["status"] == decision)
