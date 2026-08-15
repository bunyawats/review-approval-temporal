"""
Unit tests for workflow/service.py's list_reviews_page() and its
in-process query_id -> (filter, total) cache -- no live Postgres needed.

Fakes asyncpg's pool/connection interface just enough (async
`pool.acquire()` context manager yielding an object with `fetchval`/
`fetch`) to observe whether a call actually hit "Postgres" (COUNT(*) via
fetchval) or was served from the cache -- that's the behavior this test
file exists to pin down, per docs/PAGINATION_PLAN.md's caching
semantics.
"""

import pytest

from review_approval.workflow import service


class _FakeConn:
    def __init__(self, count, rows):
        self._count = count
        self._rows = rows
        self.fetchval_calls = 0
        self.fetch_calls = 0

    async def fetchval(self, query, *params):
        self.fetchval_calls += 1
        return self._count

    async def fetch(self, query, *params):
        self.fetch_calls += 1
        return self._rows


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, count=0, rows=None):
        self.conn = _FakeConn(count, rows or [])

    def acquire(self):
        return _AcquireCtx(self.conn)


@pytest.fixture(autouse=True)
def clear_query_cache():
    service._query_cache.clear()
    yield
    service._query_cache.clear()


# --------------------------------------------------------------- caching ----

async def test_filter_provided_bypasses_cache_and_mints_new_query_id_each_time():
    pool = _FakePool(count=5, rows=[{"id": "1"}])
    first = await service.list_reviews_page(pool, filter={"requester": "operator1"})
    second = await service.list_reviews_page(pool, filter={"requester": "operator1"})
    assert first.query_id != second.query_id
    assert pool.conn.fetchval_calls == 2  # a fresh COUNT(*) every time


async def test_query_id_without_filter_reuses_cached_total_and_filter():
    pool = _FakePool(count=7, rows=[])
    first = await service.list_reviews_page(pool, filter={"requester": "operator1"})
    assert pool.conn.fetchval_calls == 1

    second = await service.list_reviews_page(pool, query_id=first.query_id, page=1)

    assert pool.conn.fetchval_calls == 1  # no new COUNT(*) -- served from cache
    assert second.query_id == first.query_id
    assert second.total == 7
    assert second.filter == {"requester": "operator1", "review_type": None}
    assert second.page == 1
    assert pool.conn.fetch_calls == 2  # row data still runs live both times


async def test_unknown_query_id_without_filter_raises():
    pool = _FakePool()
    with pytest.raises(ValueError, match="query_id not found or expired"):
        await service.list_reviews_page(pool, query_id="does-not-exist")


async def test_expired_query_id_raises_rather_than_falling_back_unfiltered():
    pool = _FakePool(count=3)
    result = await service.list_reviews_page(pool, filter={"requester": "operator1"})

    filter_, total, _expires_at = service._query_cache[result.query_id]
    service._query_cache[result.query_id] = (filter_, total, 0.0)  # force expiry

    with pytest.raises(ValueError, match="query_id not found or expired"):
        await service.list_reviews_page(pool, query_id=result.query_id)


async def test_both_query_id_and_filter_omitted_mints_fresh_query_id_each_time():
    pool = _FakePool(count=2)
    first = await service.list_reviews_page(pool)
    second = await service.list_reviews_page(pool)
    assert first.query_id != second.query_id
    assert pool.conn.fetchval_calls == 2
    assert first.filter == {"requester": None, "review_type": None}


# ------------------------------------------------------------- validation ----

async def test_page_size_defaults_and_clamps_silently():
    pool = _FakePool(count=0)
    assert (await service.list_reviews_page(pool, page_size=None)).page_size == 20
    assert (await service.list_reviews_page(pool, page_size=1000)).page_size == 100
    assert (await service.list_reviews_page(pool, page_size=0)).page_size == 1


async def test_negative_page_rejected():
    pool = _FakePool()
    with pytest.raises(ValueError, match="page must be >= 0"):
        await service.list_reviews_page(pool, page=-1)


async def test_unknown_review_type_rejected():
    pool = _FakePool()
    with pytest.raises(ValueError, match="unknown review_type"):
        await service.list_reviews_page(pool, filter={"review_type": "not_a_real_type"})


async def test_known_review_type_accepted():
    pool = _FakePool(count=1)
    result = await service.list_reviews_page(pool, filter={"review_type": "purchase_order"})
    assert result.filter["review_type"] == "purchase_order"