"""
ReviewApprovalWorkflow: one execution per review request.

Deliberately payload-agnostic. review_type + payload are opaque to the
workflow -- validation of "does this payload match this review_type"
happens in the BFF before the workflow is even started/signaled. That
keeps this file stable no matter how many review types get added later.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from review_approval.workflow.activities import (
        PersistCancelInput,
        PersistDecisionInput,
        PersistRequestInput,
        PersistUpdateInput,
        persist_cancel,
        persist_decision,
        persist_request,
        persist_update,
    )

VALID_DECISIONS = ("APPROVED", "REJECTED")

DEFAULT_RETRY_POLICY = RetryPolicy(maximum_attempts=5)
DEFAULT_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@dataclass
class ReviewRequestInput:
    request_id: str
    review_type: str
    payload: dict[str, Any]
    requester: str


@dataclass
class ReviewStatus:
    status: str
    closed_by: Optional[str] = None
    closed_status: Optional[str] = None  # APPROVED | REJECTED | CANCELLED
    closed_comment: Optional[str] = None


@workflow.defn
class ReviewApprovalWorkflow:
    def __init__(self) -> None:
        self._request_id: str = ""
        self._payload: dict[str, Any] = {}
        self._status = "PENDING_REVIEW"
        self._closed_by: Optional[str] = None
        self._closed_status: Optional[str] = None
        self._closed_comment: Optional[str] = None
        self._decision_received = False
        self._cancelled = False
        self._closing = False

    def _is_final(self) -> bool:
        return self._decision_received or self._cancelled

    def _claim_final(self) -> bool:
        # Checking _is_final() and then, after an `await`, setting
        # _decision_received/_cancelled True is a check-then-act race: two
        # terminal-transition paths (a real signal and a native Temporal
        # cancel, or two real signals) can both observe _is_final() as
        # False before either has actually persisted anything, since
        # signal handlers and run()'s except block interleave at await
        # points rather than running atomically. _closing is set
        # synchronously, with no await between the check and the set, so
        # only the first caller ever gets True -- everyone else (including
        # a caller that arrives while the winner is mid-`await
        # workflow.execute_activity(persist_*)`) bails out immediately
        # instead of also scheduling a conflicting persist_* activity.
        if self._is_final() or self._closing:
            return False
        self._closing = True
        return True

    @workflow.run
    async def run(self, req: ReviewRequestInput) -> ReviewStatus:
        self._request_id = req.request_id
        self._payload = req.payload

        await workflow.execute_activity(
            persist_request,
            PersistRequestInput(
                request_id=req.request_id,
                review_type=req.review_type,
                payload=req.payload,
                requester=req.requester,
                workflow_id=workflow.info().workflow_id,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Durably waits here -- survives worker restarts, Mac reboots, etc.
        # No polling, no timers burning CPU. Woken by either signal below.
        # Persistence for both outcomes happens inside the signal handlers
        # themselves, so it's guaranteed to complete before this returns.
        #
        # A native Temporal cancel (e.g. cancelled directly via the Web UI
        # or CLI, rather than through our own cancel_request signal) is
        # delivered here as asyncio.CancelledError, not through either
        # signal handler -- persist_cancel would otherwise never run, and
        # the Postgres row would stay stuck at PENDING_REVIEW forever even
        # though Temporal considers the execution finished. Recover by
        # doing the same persistence a real cancel_request would have
        # done, attributed to Temporal itself, then let run() complete
        # normally (not re-raised) so this converges on the same
        # Completed/CANCELLED outcome cancel_request produces, instead of
        # a bare Temporal-native Canceled status.
        try:
            await workflow.wait_condition(self._is_final)
        except asyncio.CancelledError:
            if self._claim_final():
                await workflow.execute_activity(
                    persist_cancel,
                    PersistCancelInput(
                        request_id=self._request_id,
                        closed_by="temporal-admin",
                        closed_comment="forced by temporal system",
                        closed_at=workflow.now(),
                    ),
                    start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                self._cancelled = True
                self._status = "CANCELLED"
                self._closed_by = "temporal-admin"
                self._closed_status = "CANCELLED"
                self._closed_comment = "forced by temporal system"

        return ReviewStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_status=self._closed_status,
            closed_comment=self._closed_comment,
        )

    @workflow.signal
    async def submit_decision(
        self, decision: str, manager_id: str, comment: str = ""
    ) -> None:
        if not self._claim_final():
            return  # already decided/cancelled once; ignore late signals
        if decision not in VALID_DECISIONS:
            self._closing = False  # this attempt never actually finalized
            raise ApplicationError(f"invalid decision: {decision}")
        await workflow.execute_activity(
            persist_decision,
            PersistDecisionInput(
                request_id=self._request_id,
                closed_status=decision,
                closed_by=manager_id,
                closed_comment=comment,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._closed_status = decision
        self._closed_by = manager_id
        self._closed_comment = comment
        self._status = decision
        self._decision_received = True

    @workflow.signal
    async def update_payload(self, new_payload: dict[str, Any]) -> None:
        if self._is_final():
            return  # too late to edit; already decided/cancelled
        await workflow.execute_activity(
            persist_update,
            PersistUpdateInput(request_id=self._request_id, payload=new_payload),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._payload = new_payload

    @workflow.signal
    async def cancel_request(self, cancelled_by: str, comment: str = "") -> None:
        if not self._claim_final():
            return  # already decided/cancelled; nothing to cancel
        await workflow.execute_activity(
            persist_cancel,
            PersistCancelInput(
                request_id=self._request_id,
                closed_by=cancelled_by,
                closed_comment=comment,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._cancelled = True
        self._status = "CANCELLED"
        self._closed_by = cancelled_by
        self._closed_status = "CANCELLED"
        self._closed_comment = comment

    @workflow.query
    def get_status(self) -> ReviewStatus:
        return ReviewStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_status=self._closed_status,
            closed_comment=self._closed_comment,
        )
