# Paginated, count-cached review-request listing

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (no dependency on `~/.claude/plans/`, which isn't part of this
> repo and isn't guaranteed to survive across machines/sessions).
>
> - [ ] Phase 1 — schema indexes, `workflow/service.py` pagination +
>       count-cache core, `POST /reviews/search` on the REST API, tests
> - [ ] Phase 2 — BFF wiring (`/ui/operator/list`, `/ui/manager/list`),
>       pagination controls in the templates, 5s poll carries
>       `query_id`/`page`
> - [ ] Phase 3 — cleanup: remove the now-superseded `list_reviews()`,
>       update `CLAUDE.md`/`README.md` to describe the final shape

## Context

`workflow/service.py`'s `list_reviews(pool, requester=None)` runs an
unpaginated `SELECT * FROM review_requests [WHERE requester = $1]
ORDER BY created_at DESC` on every call, with no `LIMIT`/`OFFSET` and no
`COUNT(*)` anywhere in the codebase. It's called from `GET /reviews`
(REST) and four BFF routes — including `/ui/operator/list` and
`/ui/manager/list`, which the operator/manager screens self-poll every 5
seconds via htmx (`_operator_list.html`, `_manager_list.html`) for as
long as the tab stays open. That poll is the actual hot path this design
optimizes: every 5 seconds, every open tab re-runs a full unfiltered (or
requester-filtered) table scan with no supporting index on `requester`
or `created_at` (only `status` and `review_type` are indexed today).

This plan adds a paginated, count-cached listing endpoint plus the
indexes the query pattern has always needed, staged as **3 independent,
each-independently-mergeable phases**, meant to be done one session at a
time — a session picking this up mid-plan should read this file, check
which phase's files already exist/are wired up, and resume from there.

**Ordering rationale**: Phase 1 lands the REST API and the service-layer
primitives in isolation, alongside the *existing* `list_reviews()`
(left untouched) — so the BFF keeps working unmodified until Phase 2
explicitly migrates it. This mirrors `keycloak/INTEGRATION_PLAN.md`'s
approach of keeping every phase's commit fully working on its own.

## API specification (confirmed with user)

```
POST /reviews/search
Content-Type: application/json

{
  "page": null,
  "page_size": null,
  "query_id": null,
  "filter": {
    "requester": null,
    "review_type": null
  }
}
```

**Request fields:**
- `page` — nullable int, null/omitted → `0`. Negative → `400`.
- `page_size` — nullable int, null/omitted → `20`. Clamped silently
  into `[1, 100]` (no error) — the response's `page_size` reflects the
  clamped value actually used, so the client can tell it happened just
  by comparing what it sent to what came back.
- `query_id` — nullable string (uuid4). See caching semantics below.
- `filter` — nullable object. `filter.requester` — nullable string,
  exact match, unvalidated. `filter.review_type` — nullable string,
  exact match, validated against `KNOWN_REVIEW_TYPES`
  (`workflow/task_queues.py`) — unknown value → `400`, same as
  `validate_payload()`'s existing behavior for unknown review types.

**Response:**
```json
{
  "query_id": "3fa2b1c4-5b6a-4e1f-9c3d-...",
  "page": 0,
  "page_size": 20,
  "filter": { "requester": "operator1", "review_type": "leave_request" },
  "total": 12,
  "items": [ { "id": "...", "review_type": "...", "status": "...", ... }, ... ]
}
```
`filter` is echoed back (post-defaulting) alongside `page_size`, purely
for debuggability — the client can always see exactly what was applied.

**Caching semantics** — POST method chosen (over `GET` + query string)
specifically so all of the above lives in a JSON body rather than a URL;
`POST /reviews` was already taken by request creation, hence the
`/reviews/search` path. Only the *total count* is ever cached, never row
data — row data (`SELECT ... LIMIT/OFFSET`) always runs live, every
call:

- **`filter` provided (non-null)** → cache is bypassed entirely: fresh
  `COUNT(*)`, a new `query_id` minted every time. The client is
  explicitly stating what to filter on, so it's trusted directly rather
  than checked against anything cached.
- **`filter` omitted/null, `query_id` provided** → look up the cache
  entry by `query_id` and pull *both* the filter and the total from it
  (they're stored together at creation time). No recompute needed —
  finding the entry at all means it hasn't expired. **If the id isn't
  found (expired or unknown) → `400`** ("query_id not found or expired
  — resend filter to start a new query"). This does *not* silently fall
  back to unfiltered — silently dropping a filter risks exposing more
  rows than the caller intended, which is a correctness/exposure risk,
  not just a perf hiccup the way a stale count would be.
- **Both omitted** → unfiltered, fresh `COUNT(*)`, new `query_id`
  minted, same as the "both provided" case functionally (no filter to
  bypass).
- Cache: in-process TTL dict in `workflow/service.py`, 30s TTL, no new
  dependency (no Redis) — keyed `query_id → (filter, total,
  expires_at)`. Deliberately self-healing across Kubernetes replicas: a
  `query_id` minted on one pod is just a miss on another pod, never a
  wrong answer — so it's correct today even before any shared-cache
  upgrade, which would be a future option, not built now (same
  "deliberate simplification" pattern as the UMA permission-check
  caching gap already documented in `CLAUDE.md`'s "Known gaps").

**BFF note for Phase 2**: the BFF must always send `filter.requester`
explicitly on every poll — never relies on the cached-filter path above.
The operator-visibility invariant (`requester == session user`) has to
hold even after a cache entry expires mid-session; relying on the cache
to remember the filter would break that invariant the moment the TTL
lapses.

**New indexes** (the actual query-optimization half of this plan):
```sql
CREATE INDEX idx_review_requests_requester_created_at
  ON review_requests (requester, created_at DESC);
CREATE INDEX idx_review_requests_created_at
  ON review_requests (created_at DESC);
```

---

## Phase 1 — Schema + service layer + REST API

**Goal**: `POST /reviews/search` returns paginated results with a
working count cache, verified by unit tests (cache logic, no live
services) and integration tests (real Postgres). This alone gives any
REST client — and, later, the BFF — a real paginated primitive to build
on.

**Files**:
- `db/schema.sql` — the two new indexes above.
- `workflow/service.py` — new `PagedReviews` dataclass (`query_id`,
  `page`, `page_size`, `filter`, `total`, `items`); new
  `list_reviews_page(pool, page=0, page_size=20, query_id=None,
  filter=None)` implementing the pagination + count-cache algorithm
  above. The existing `list_reviews()` is left **untouched** — BFF keeps
  calling it unchanged until Phase 2.
- `api/routes.py` — new `POST /reviews/search` route: a Pydantic request
  model matching the body above, calls `service.list_reviews_page()`,
  maps `ValueError` (negative page, unknown `query_id`, unknown
  `review_type`) to `400` the same way this file already maps
  `service.py` exceptions elsewhere.
- `README.md` — new "Try it" example for `POST /reviews/search`
  alongside the existing create/decision/cancel curl examples.

**Tests**:
- `tests/unit/` — cache hit (matching `query_id` + no `filter` reuses
  stored total+filter), cache miss (`filter` provided bypasses cache;
  unknown/expired `query_id` with no `filter` → `400`), `page_size`
  clamping, negative `page` rejection, unknown `review_type` rejection.
  No live services.
- `tests/integration/` — against real Postgres: verifies a repeat call
  with a valid `query_id` actually skips `COUNT(*)` (e.g. via query
  count/timing assertions or a monkeypatched counter), pagination
  ordering/correctness across multiple pages, and that `filter.requester`
  + `filter.review_type` narrow results correctly.

---

## Phase 2 — BFF wiring

**Goal**: `/ui/operator` and `/ui/manager` (plus their `/list`
polling fragments) use `list_reviews_page()` instead of the unpaginated
`list_reviews()`, with real pagination controls and a poll that actually
benefits from the count cache instead of reinventing a fresh `query_id`
every 5 seconds.

**Files**:
- `bff/ui.py` — `/ui/operator`, `/ui/operator/list`, `/ui/manager`,
  `/ui/manager/list` migrate to `list_reviews_page()`. Operator routes
  always pass `filter={"requester": user["username"]}` explicitly (see
  the BFF note above — never rely on the cached-filter path). Manager
  routes pass no `requester` filter (unchanged visibility).
- `bff/templates/_operator_list.html`, `_manager_list.html` — add
  Prev/Next controls; the self-polling `<table>` element carries the
  current `page` and `query_id` as data-attributes, resent as hidden
  `hx-post` form fields on each 5s poll trigger, so (a) the poll reuses
  the cached count instead of minting a new `query_id` every cycle, and
  (b) the poll doesn't silently reset the user back to page 0 while
  they're browsing a later page.
- `bff/templates/_operator_row.html`, `_manager_row.html` — no change
  expected (row rendering is unaffected by pagination), but verify
  Save/Cancel/Approve/Reject's single-row re-render still integrates
  cleanly with a paginated list (a decided row's page shouldn't shift
  out from under the user mid-action).

**Tests**: extend the existing BFF integration test pattern
(`tests/integration/test_bff_*.py`) to cover multi-page operator/manager
listing and that the poll's `query_id`/`page` round-trip correctly.

---

## Phase 3 — Cleanup

**Goal**: no leftover unpaginated code path, docs match reality.

- Remove `workflow/service.py`'s old `list_reviews()` once nothing calls
  it (confirm via grep across `api/` and `bff/`).
- `CLAUDE.md`/`README.md` — update to describe the final implemented
  shape (endpoint contract, indexes, cache behavior) and note the
  accepted limitation: count-cache staleness is TTL-bounded (30s), not
  event-invalidated on writes — a deliberate simplification, not an
  oversight, matching this project's existing "no caching in the first
  pass" pattern for UMA permission checks.
- Sweep for any remaining reference to the old unpaginated
  `list_reviews()` signature or bare-array `GET /reviews`-style response
  shape in comments/docstrings, to confirm nothing was missed.
