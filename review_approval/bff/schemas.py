"""
Registry of payload schemas per review_type.

Adding a new review type touches TWO places, not one:
  1. This file -- add a Pydantic model and a REVIEW_TYPE_SCHEMAS entry.
  2. review_approval/task_queues.py -- add the type string to
     KNOWN_REVIEW_TYPES.
The assertion below fails loudly at import time if these two drift apart,
rather than silently starting workflows on a task queue nothing polls.
"""

from typing import Any

from pydantic import BaseModel

from review_approval.task_queues import KNOWN_REVIEW_TYPES


class PurchaseOrderPayload(BaseModel):
    vendor: str
    amount: float
    currency: str = "USD"
    line_items: list[str] = []


class LeaveRequestPayload(BaseModel):
    employee: str
    start_date: str
    end_date: str
    reason: str


# Add new review types here (and to KNOWN_REVIEW_TYPES in task_queues.py).
REVIEW_TYPE_SCHEMAS: dict[str, type[BaseModel]] = {
    "purchase_order": PurchaseOrderPayload,
    "leave_request": LeaveRequestPayload,
}

assert set(REVIEW_TYPE_SCHEMAS) == set(KNOWN_REVIEW_TYPES), (
    f"REVIEW_TYPE_SCHEMAS {set(REVIEW_TYPE_SCHEMAS)} and KNOWN_REVIEW_TYPES "
    f"{set(KNOWN_REVIEW_TYPES)} have drifted apart -- update both when "
    f"adding or removing a review type."
)


def validate_payload(review_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = REVIEW_TYPE_SCHEMAS.get(review_type)
    if schema is None:
        raise ValueError(
            f"unknown review_type '{review_type}'. "
            f"Known types: {list(REVIEW_TYPE_SCHEMAS)}"
        )
    return schema(**payload).model_dump()
