"""
Unit tests for workflow/service.py's bulk primitives
(bulk_cancel_reviews/bulk_submit_decision/_validate_bulk_ids) -- no live
Postgres/Temporal needed.

bulk_cancel_reviews()/bulk_submit_decision() are thin orchestration layers
over cancel_review()/submit_decision() (see docs/BULK_ACTIONS_PLAN.md) --
these tests monkeypatch those single-item functions rather than faking the
full Temporal/Postgres stack, since what's actually under test here is the
fan-out/collection/exception-handling behavior, not the single-item
functions' own correctness (covered separately by
tests/integration/test_api_permissions.py).
"""

import pytest

from review_approval.workflow import service


# -------------------------------------------------------- _validate_bulk_ids ----

def test_validate_bulk_ids_dedupes_preserving_order():
    assert service._validate_bulk_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_validate_bulk_ids_empty_raises():
    with pytest.raises(ValueError, match="must not be empty"):
        service._validate_bulk_ids([])


def test_validate_bulk_ids_over_cap_raises():
    ids = [str(i) for i in range(service._MAX_BULK_SIZE + 1)]
    with pytest.raises(ValueError, match="at most 50 requests"):
        service._validate_bulk_ids(ids)


def test_validate_bulk_ids_at_cap_accepted():
    ids = [str(i) for i in range(service._MAX_BULK_SIZE)]
    assert service._validate_bulk_ids(ids) == ids


# ------------------------------------------------------- bulk_cancel_reviews ----

async def test_bulk_cancel_reviews_collects_per_item_results(monkeypatch):
    async def fake_cancel_review(client, pool, request_id, requester, comment):
        if request_id == "bad":
            raise ValueError("this review is no longer cancellable")
        return None

    monkeypatch.setattr(service, "cancel_review", fake_cancel_review)

    results = await service.bulk_cancel_reviews(
        client=None, pool=None, request_ids=["good1", "bad", "good2"], requester="operator1"
    )

    by_id = {r.request_id: r for r in results}
    assert by_id["good1"].ok and by_id["good1"].error is None
    assert by_id["good2"].ok and by_id["good2"].error is None
    assert not by_id["bad"].ok
    assert "no longer cancellable" in by_id["bad"].error
    # asyncio.gather preserves input order
    assert [r.request_id for r in results] == ["good1", "bad", "good2"]


async def test_bulk_cancel_reviews_catches_lookup_and_permission_errors_too(monkeypatch):
    async def fake_cancel_review(client, pool, request_id, requester, comment):
        if request_id == "missing":
            raise LookupError("review not found")
        if request_id == "not-mine":
            raise PermissionError("only the requester who created this review can cancel it")
        return None

    monkeypatch.setattr(service, "cancel_review", fake_cancel_review)

    results = await service.bulk_cancel_reviews(
        client=None, pool=None, request_ids=["missing", "not-mine"], requester="operator1"
    )
    assert all(not r.ok for r in results)


async def test_bulk_cancel_reviews_does_not_swallow_unexpected_exceptions(monkeypatch):
    async def fake_cancel_review(client, pool, request_id, requester, comment):
        raise RuntimeError("genuine bug")

    monkeypatch.setattr(service, "cancel_review", fake_cancel_review)

    with pytest.raises(RuntimeError, match="genuine bug"):
        await service.bulk_cancel_reviews(client=None, pool=None, request_ids=["x"], requester="operator1")


async def test_bulk_cancel_reviews_rejects_empty_list():
    with pytest.raises(ValueError, match="must not be empty"):
        await service.bulk_cancel_reviews(client=None, pool=None, request_ids=[], requester="operator1")


# ----------------------------------------------------- bulk_submit_decision ----

async def test_bulk_submit_decision_validates_decision_before_looping():
    with pytest.raises(ValueError, match="APPROVED or REJECTED"):
        await service.bulk_submit_decision(
            client=None, pool=None, request_ids=["a"], decision="MAYBE", closed_by="manager1"
        )


async def test_bulk_submit_decision_collects_per_item_results(monkeypatch):
    async def fake_submit_decision(client, pool, request_id, decision, closed_by, comment):
        if request_id == "already-decided":
            raise ValueError("this review has already been decided")
        return None

    monkeypatch.setattr(service, "submit_decision", fake_submit_decision)

    results = await service.bulk_submit_decision(
        client=None,
        pool=None,
        request_ids=["ok1", "already-decided", "ok2"],
        decision="APPROVED",
        closed_by="manager1",
    )

    by_id = {r.request_id: r for r in results}
    assert by_id["ok1"].ok and by_id["ok2"].ok
    assert not by_id["already-decided"].ok
