"""
Registry of payload schemas per review_type.

Adding a new review type touches TWO places, not one:
  1. This file -- add a Pydantic model, a REVIEW_TYPE_SCHEMAS entry, and a
     SAMPLE_PAYLOADS entry (used to auto-populate the "New Request" form).
  2. review_approval/task_queues.py -- add the type string to
     KNOWN_REVIEW_TYPES.
The assertions below fail loudly at import time if these drift apart,
rather than silently starting workflows on a task queue nothing polls, or
serving a mockup dialog with no sample for a given type.
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

# One realistic example payload per review_type, used only to pre-fill the
# "New Request" dialog's textarea -- never validated or persisted as-is.
SAMPLE_PAYLOADS: dict[str, dict[str, Any]] = {
    "purchase_order": {
        "vendor": "Acme Corp",
        "amount": 1500.00,
        "currency": "USD",
        "line_items": ["widgets", "gadgets"],
    },
    "leave_request": {
        "employee": "Jane Doe",
        "start_date": "2026-08-10",
        "end_date": "2026-08-14",
        "reason": "Family vacation",
    },
}

assert set(SAMPLE_PAYLOADS) == set(KNOWN_REVIEW_TYPES), (
    f"SAMPLE_PAYLOADS {set(SAMPLE_PAYLOADS)} and KNOWN_REVIEW_TYPES "
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
