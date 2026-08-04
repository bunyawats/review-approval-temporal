"""
Run this as its own long-lived process:

    python -m review_approval.worker

It's what actually executes workflow/activity code. The BFF only ever
talks to the Temporal server, never to this process directly.

Two independent env vars control what this process does -- combine them
freely, same image, same file, no code duplication:

WORKER_MODE ("both" / "workflow" / "activity", default "both") -- which
half of the work this process registers:
  - "both"     -- registers workflow + activities. Simplest, fine for
                local/native runs and small deployments.
  - "workflow" -- registers ONLY the workflow. Never touches Postgres or
                needs DATABASE_URL (workflow code is pure/deterministic --
                no I/O), so this process can run with zero DB credentials.
  - "activity" -- registers ONLY the activities. Needs DATABASE_URL.

REVIEW_TYPE (unset by default) -- which review-type-specific task queue(s)
this process polls:
  - unset      -- polls EVERY known review type's queue (one Worker per
                type, run concurrently in this one process). This is the
                "just run it, no config" path for local/native dev.
  - set        -- polls ONLY that review type's queue (e.g.
                REVIEW_TYPE=purchase_order). This is what lets you scale
                worker capacity independently per review type: give
                purchase_order its own pod/replica count in Kubernetes
                without touching leave_request's.

Multiple Worker processes (any mix of modes, any mix of review types) can
poll the SAME task queue simultaneously -- Temporal dispatches workflow
tasks and activity tasks separately, so a "workflow"-mode worker simply
never receives activity tasks, and vice versa.
"""

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from review_approval.activities import (
    persist_cancel,
    persist_decision,
    persist_request,
    persist_update,
)
from review_approval.task_queues import KNOWN_REVIEW_TYPES, task_queue_for_review_type
from review_approval.workflows import ReviewApprovalWorkflow

VALID_MODES = ("both", "workflow", "activity")


def _build_worker(client: Client, task_queue: str, mode: str) -> Worker:
    workflows = [ReviewApprovalWorkflow] if mode in ("both", "workflow") else []
    activities = (
        [persist_request, persist_decision, persist_update, persist_cancel]
        if mode in ("both", "activity")
        else []
    )
    print(
        f"Worker registering on task queue '{task_queue}' (mode={mode}) "
        f"[workflows={len(workflows)}, activities={len(activities)}]"
    )
    return Worker(client, task_queue=task_queue, workflows=workflows, activities=activities)


async def main() -> None:
    mode = os.environ.get("WORKER_MODE", "both")
    if mode not in VALID_MODES:
        raise SystemExit(f"WORKER_MODE={mode!r} invalid, must be one of {VALID_MODES}")

    review_type = os.environ.get("REVIEW_TYPE")
    if review_type is not None and review_type not in KNOWN_REVIEW_TYPES:
        raise SystemExit(
            f"REVIEW_TYPE={review_type!r} unknown, must be one of {KNOWN_REVIEW_TYPES} "
            f"(or unset, to poll all of them)"
        )

    client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )

    review_types = [review_type] if review_type else list(KNOWN_REVIEW_TYPES)
    workers = [
        _build_worker(client, task_queue_for_review_type(rt), mode) for rt in review_types
    ]

    print(f"Worker process started, serving review types: {review_types}")
    await asyncio.gather(*(w.run() for w in workers))


if __name__ == "__main__":
    asyncio.run(main())
