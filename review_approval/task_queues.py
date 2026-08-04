"""
Task queue naming and the canonical list of known review types.

Shared by bff/service.py (starts each workflow on a review-type-specific
queue) and worker.py (polls one such queue per process, or all of them
for a general-purpose worker). This is the single source of truth for
"what review types exist," so the producer side (BFF) and consumer side
(worker) can't drift out of sync on the naming scheme.

Deliberately has ZERO dependency on bff/ -- worker.py imports this
directly and must never need to import anything from bff/. bff/schemas.py
imports FROM here (that direction is fine; bff depending on core is the
established pattern throughout this project, the reverse isn't).
"""

# Every review_type the system knows about. bff/schemas.py asserts its own
# REVIEW_TYPE_SCHEMAS registry has exactly these keys -- if you add a new
# review type, you MUST update both this tuple and that registry, or the
# assertion in bff/schemas.py will fail loudly at import time rather than
# silently routing workflows to a queue nothing is polling.
KNOWN_REVIEW_TYPES = ("purchase_order", "leave_request")


def task_queue_for_review_type(review_type: str) -> str:
    return f"review-approval-{review_type}-task-queue"
