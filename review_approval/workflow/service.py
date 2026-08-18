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
from dataclasses import dataclass
from typing import Any, Callable, Optional

import asyncpg
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from review_approval.workflow.schemas import validate_payload
from review_approval.workflow.task_queues import KNOWN_REVIEW_TYPES, task_queue_for_review_type
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


_QUERY_CACHE_TTL_S = 30.0
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100

# query_id -> (filter, total, expires_at). In-process only, deliberately --
# a query_id minted on one Kubernetes replica is just a cache miss on
# another, never a wrong answer, so this is correct today without a shared
# cache (Redis etc.), which would be a future option, not needed now. Same
# "deliberate simplification" as the UMA permission-check caching gap noted
# in CLAUDE.md's "Known gaps".
_query_cache: dict[str, tuple[dict[str, Optional[str]], int, float]] = {}


@dataclass
class PagedReviews:
    query_id: str
    page: int
    page_size: int
    filter: dict[str, Optional[str]]
    total: int
    items: list[dict]


def _cache_get(query_id: str) -> Optional[tuple[dict[str, Optional[str]], int]]:
    entry = _query_cache.get(query_id)
    if entry is None:
        return None
    filter_, total, expires_at = entry
    if time.monotonic() >= expires_at:
        _query_cache.pop(query_id, None)
        return None
    return filter_, total


def _cache_put(filter_: dict[str, Optional[str]], total: int) -> str:
    query_id = str(uuid.uuid4())
    _query_cache[query_id] = (filter_, total, time.monotonic() + _QUERY_CACHE_TTL_S)
    return query_id


def _where_clause(filter_: dict[str, Optional[str]]) -> tuple[str, list]:
    conditions = []
    params: list = []
    if filter_.get("requester"):
        params.append(filter_["requester"])
        conditions.append(f"requester = ${len(params)}")
    if filter_.get("review_type"):
        params.append(filter_["review_type"])
        conditions.append(f"review_type = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


async def _count_reviews(pool: asyncpg.Pool, filter_: dict[str, Optional[str]]) -> int:
    where, params = _where_clause(filter_)
    async with pool.acquire() as conn:
        return await conn.fetchval(f"SELECT COUNT(*) FROM review_requests {where}", *params)


async def _fetch_reviews_page(
    pool: asyncpg.Pool, filter_: dict[str, Optional[str]], page: int, page_size: int
) -> list[dict]:
    where, params = _where_clause(filter_)
    params.extend([page_size, page * page_size])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM review_requests {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return [dict(r) for r in rows]


async def list_reviews_page(
    pool: asyncpg.Pool,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    query_id: Optional[str] = None,
    filter: Optional[dict[str, Optional[str]]] = None,
) -> PagedReviews:
    page = 0 if page is None else page
    if page < 0:
        raise ValueError("page must be >= 0")
    page_size = _DEFAULT_PAGE_SIZE if page_size is None else page_size
    page_size = max(1, min(page_size, _MAX_PAGE_SIZE))

    if filter:
        review_type = filter.get("review_type")
        if review_type is not None and review_type not in KNOWN_REVIEW_TYPES:
            raise ValueError(f"unknown review_type: {review_type}")
        resolved_filter: dict[str, Optional[str]] = {
            "requester": filter.get("requester"),
            "review_type": review_type,
        }
        total = await _count_reviews(pool, resolved_filter)
        resolved_query_id = _cache_put(resolved_filter, total)
    elif query_id:
        cached = _cache_get(query_id)
        if cached is None:
            raise ValueError("query_id not found or expired -- resend filter to start a new query")
        resolved_filter, total = cached
        resolved_query_id = query_id
    else:
        resolved_filter = {"requester": None, "review_type": None}
        total = await _count_reviews(pool, resolved_filter)
        resolved_query_id = _cache_put(resolved_filter, total)

    items = await _fetch_reviews_page(pool, resolved_filter, page, page_size)
    return PagedReviews(
        query_id=resolved_query_id,
        page=page,
        page_size=page_size,
        filter=resolved_filter,
        total=total,
        items=items,
    )


async def get_review(pool: asyncpg.Pool, request_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM review_requests WHERE id = $1", request_id
        )
    return dict(row) if row else None


async def _reconcile_missing_workflow(pool: asyncpg.Pool, request_id: str) -> None:
    """Recover from a Temporal admin deleting the workflow execution out from
    under a still-PENDING_REVIEW row (`temporal workflow delete`, not just a
    cancel/terminate) -- there is no workflow left to signal, and no
    execution left to host a persist_cancel activity through, so this writes
    directly to Postgres instead of going through the normal
    workflow/activity path (the one deliberate exception to "activities.py
    is the only file allowed to talk to Postgres": that rule is about
    durable writes going through Temporal, which is meaningless once
    Temporal's own record of the execution no longer exists).

    closed_at is left NULL, not now() -- unlike workflows.py's native-cancel
    handling, there's no Temporal-side event to anchor a real timestamp to;
    we only found out the workflow was gone whenever this happened to run.

    workflow_id is also cleared to NULL -- it can no longer be associated
    with any real Temporal execution (nothing to `get_workflow_handle()`
    against), so keeping the old id around would be misleading rather than
    just unavailable.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE review_requests
            SET status = 'CANCELLED',
                closed_status = 'CANCELLED',
                closed_by = 'temporal-admin',
                closed_comment = 'workflow transaction not found',
                closed_at = NULL,
                workflow_id = NULL
            WHERE id = $1 AND status = 'PENDING_REVIEW'
            """,
            request_id,
        )


async def _signal_or_reconcile(
    pool: asyncpg.Pool, handle, request_id: str, signal, args: list
) -> None:
    """Shared by every signal-sending mutation below: send the signal, but
    if Temporal reports the workflow no longer exists (RPCError NOT_FOUND --
    see _reconcile_missing_workflow's docstring for why), self-heal the row
    instead of letting a raw RPCError surface as an unhandled 500.
    """
    try:
        await handle.signal(signal, args=args)
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        await _reconcile_missing_workflow(pool, request_id)
        raise ValueError(
            "this review's Temporal workflow no longer exists -- it has been marked cancelled"
        )


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
    await _signal_or_reconcile(
        pool, handle, request_id, ReviewApprovalWorkflow.update_payload, [validated]
    )
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
    await _signal_or_reconcile(
        pool,
        handle,
        request_id,
        ReviewApprovalWorkflow.cancel_request,
        [requester, comment],
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
    await _signal_or_reconcile(
        pool,
        handle,
        request_id,
        ReviewApprovalWorkflow.submit_decision,
        [decision, closed_by, comment],
    )
    # signal() only confirms Temporal accepted it -- wait for persist_decision
    # to actually write the new status before returning.
    await _wait_until(pool, request_id, lambda record: record["status"] == decision)
