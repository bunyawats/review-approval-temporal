"""
Unit tests for workflow/memory_service.py's pure logic -- serialization
and the mutation helpers -- none of which need a live Redis. The actual
Redis I/O (load/save/delete) is covered by
tests/integration/test_memory_service.py instead, against a real
instance.
"""

import time

from review_approval.workflow.memory_service import PaginationMemory, SessionMemory


def test_empty_session_memory_round_trips_through_json():
    memory = SessionMemory()

    restored = SessionMemory.from_json(memory.to_json())

    assert restored == SessionMemory(pagination=None, bulk_selection=[])


def test_populated_session_memory_round_trips_through_json():
    memory = SessionMemory(
        pagination=PaginationMemory(
            query_id="q-1", filter={"requester": "operator1", "review_type": None}, total=7, cached_at=1234.5
        ),
        bulk_selection=["req-1", "req-2"],
    )

    restored = SessionMemory.from_json(memory.to_json())

    assert restored == memory


def test_select_is_idempotent():
    memory = SessionMemory()

    memory.select("req-1")
    memory.select("req-1")

    assert memory.bulk_selection == ["req-1"]


def test_deselect_missing_id_is_a_noop():
    memory = SessionMemory(bulk_selection=["req-1"])

    memory.deselect("req-does-not-exist")

    assert memory.bulk_selection == ["req-1"]


def test_deselect_removes_id():
    memory = SessionMemory(bulk_selection=["req-1", "req-2"])

    memory.deselect("req-1")

    assert memory.bulk_selection == ["req-2"]


def test_clear_selection_empties_list_but_leaves_pagination_untouched():
    pagination = PaginationMemory(query_id="q-1", filter={}, total=1, cached_at=time.time())
    memory = SessionMemory(pagination=pagination, bulk_selection=["req-1"])

    memory.clear_selection()

    assert memory.bulk_selection == []
    assert memory.pagination == pagination


def test_set_pagination_overwrites_previous_value_with_a_fresh_timestamp():
    memory = SessionMemory()
    before = time.time()

    memory.set_pagination("q-2", {"requester": "operator1", "review_type": None}, 3)

    assert memory.pagination.query_id == "q-2"
    assert memory.pagination.total == 3
    assert memory.pagination.cached_at >= before


def test_pagination_memory_is_stale_after_max_age():
    pagination = PaginationMemory(query_id="q-1", filter={}, total=1, cached_at=time.time() - 100)

    assert pagination.is_stale(30) is True
    assert pagination.is_stale(200) is False
