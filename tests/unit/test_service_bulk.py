"""
Unit tests for workflow/service.py's bulk primitive
(bulk_submit_decision/_validate_bulk_ids) -- no live Postgres/Temporal
needed.

bulk_submit_decision() is a thin orchestration layer over
submit_decision() (see docs/BULK_ACTIONS_PLAN.md,
docs/MERGE_CANCEL_DECISION_PLAN.md -- cancelling used to be a separate
bulk_cancel_reviews()/cancel_review() pair, merged into this single
function/decision value) -- these tests monkeypatch that single-item
function rather than faking the full Temporal/Postgres stack, since
what's actually under test here is the fan-out/collection/exception-
handling behavior, not the single-item function's own correctness
(covered separately by tests/integration/test_api_permissions.py).
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


# ----------------------------------------------------- bulk_submit_decision ----

async def test_bulk_submit_decision_validates_decision_before_looping():
    with pytest.raises(ValueError, match="decision must be one of"):
        await service.bulk_submit_decision(
            client=None, pool=None, request_ids=["a"], decision="MAYBE", actor="manager1"
        )


async def test_bulk_submit_decision_rejects_empty_list():
    with pytest.raises(ValueError, match="must not be empty"):
        await service.bulk_submit_decision(
            client=None, pool=None, request_ids=[], decision="APPROVED", actor="manager1"
        )


async def test_bulk_submit_decision_collects_per_item_results(monkeypatch):
    async def fake_submit_decision(client, pool, request_id, decision, actor, comment):
        if request_id == "already-decided":
            raise ValueError("this review has already been decided")
        return None

    monkeypatch.setattr(service, "submit_decision", fake_submit_decision)

    results = await service.bulk_submit_decision(
        client=None,
        pool=None,
        request_ids=["ok1", "already-decided", "ok2"],
        decision="APPROVED",
        actor="manager1",
    )

    by_id = {r.request_id: r for r in results}
    assert by_id["ok1"].ok and by_id["ok2"].ok
    assert not by_id["already-decided"].ok
    # asyncio.gather preserves input order
    assert [r.request_id for r in results] == ["ok1", "already-decided", "ok2"]


async def test_bulk_submit_decision_catches_lookup_and_permission_errors_too(monkeypatch):
    async def fake_submit_decision(client, pool, request_id, decision, actor, comment):
        if request_id == "missing":
            raise LookupError("review not found")
        if request_id == "not-mine":
            raise PermissionError("only the requester who created this review can cancel it")
        return None

    monkeypatch.setattr(service, "submit_decision", fake_submit_decision)

    results = await service.bulk_submit_decision(
        client=None, pool=None, request_ids=["missing", "not-mine"], decision="CANCELLED", actor="operator1"
    )
    assert all(not r.ok for r in results)


async def test_bulk_submit_decision_cancelled_collects_per_item_results(monkeypatch):
    # decision="CANCELLED" is the merged replacement for the old, separate
    # bulk_cancel_reviews() -- same fan-out/collection behavior, just
    # routed through submit_decision() with a different decision value.
    async def fake_submit_decision(client, pool, request_id, decision, actor, comment):
        assert decision == "CANCELLED"
        if request_id == "bad":
            raise ValueError("this review has already been decided")
        return None

    monkeypatch.setattr(service, "submit_decision", fake_submit_decision)

    results = await service.bulk_submit_decision(
        client=None, pool=None, request_ids=["good1", "bad", "good2"], decision="CANCELLED", actor="operator1"
    )

    by_id = {r.request_id: r for r in results}
    assert by_id["good1"].ok and by_id["good1"].error is None
    assert by_id["good2"].ok and by_id["good2"].error is None
    assert not by_id["bad"].ok
    assert "already been decided" in by_id["bad"].error


async def test_bulk_submit_decision_does_not_swallow_unexpected_exceptions(monkeypatch):
    async def fake_submit_decision(client, pool, request_id, decision, actor, comment):
        raise RuntimeError("genuine bug")

    monkeypatch.setattr(service, "submit_decision", fake_submit_decision)

    with pytest.raises(RuntimeError, match="genuine bug"):
        await service.bulk_submit_decision(client=None, pool=None, request_ids=["x"], decision="APPROVED", actor="manager1")
