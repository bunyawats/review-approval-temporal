# Per-session UI memory (Redis) for pagination fallback + bulk selection

> **Status tracker** — update this block as each phase's steps land, so a
> new session can tell exactly where to resume just by reading this file
> (same convention as `docs/SESSION_STORE_PLAN.md`,
> `docs/PAGINATION_PLAN.md`, `keycloak/INTEGRATION_PLAN.md`).
>
> - [ ] Phase 1 — `workflow/memory_service.py` (new module: the
>       `SessionMemory` class + Redis interaction against `ui-memory:<id>`),
>       wired into `bff/keycloak_session.py`'s `logout()` cleanup, no
>       other behavior change yet
> - [ ] Phase 2 — bulk selection moves from `bff/ui.py`'s in-process
>       `_bulk_selection` dict to `SessionMemory`
> - [ ] Phase 3 — pagination resilience fallback: `_resolve_operator_page()`/
>       `_resolve_manager_page()` consult session memory as a last-resort
>       tier before recomputing from scratch, and refresh it whenever they
>       mint a genuinely new query
> - [ ] Phase 4 — tests, docs (`CLAUDE.md`, this file's own status)

## Context

Two pieces of state currently live in-process, called out in existing
code comments as candidates for this exact move once a Redis session
store existed (`docs/SESSION_STORE_PLAN.md`, now shipped — see
`bff/session_store.py`):

1. **`workflow/service.py`'s `_query_cache`** — `query_id -> (filter,
   total, expires_at)`, a 30-second TTL cache backing `list_reviews_page()`
   so a poll/Prev/Next round-trip doesn't re-run `COUNT(*)` every cycle.
   Used by **both** front doors: the REST API's `POST /reviews/search`
   and the BFF's `/ui/operator`/`/ui/manager` list screens.
2. **`bff/ui.py`'s `_bulk_selection`** — `username -> set of selected
   request_ids`, backing the bulk cancel/approve/reject selection UI.
   BFF-only.

**Why these can't both become one "delete on logout" session object,
verbatim as first framed**: `_query_cache` is shared infrastructure the
REST API depends on, and REST API callers are stateless bearer-token
clients with **no login session and no logout** to hang a "delete on
logout" lifecycle off of. Moving it wholesale into a per-session BFF
object would either break the REST API's own caching or require
`workflow/service.py` to depend on `bff/`'s session concept — a direct
violation of this codebase's one-way `workflow/` → never `api/`/`bff/`
dependency rule (see `CLAUDE.md`'s Architecture section).

**Resolution** (confirmed with the user before designing the shape
below): `workflow/service.py`'s `_query_cache` stays exactly as it is —
untouched, still shared, still in-process, still serving the REST API
unaffected by any of this. The new per-session Redis memory holds a
**resilience fallback** for the BFF's own pagination — recovering
`filter`/`total`/`query_id` context for *this session* if the client's
own hidden `query_id` field is missing/expired or the shared
`_query_cache` entry isn't on the replica that answers the request —
never the authoritative source for *which page to render*. The
request's own `page`/`query_id` params remain authoritative exactly as
today; multi-tab and browser back/forward keep working unchanged. This
also means `_resolve_operator_page()`/`_resolve_manager_page()`'s
existing "unknown query_id falls back gracefully" behavior (already
tested — `test_operator_unknown_query_id_falls_back_gracefully`) is
preserved as the final fallback tier, not replaced.

Bulk selection has no such conflict — it's BFF-only already, and moving
it to be keyed by session id (rather than username) is a strict
refinement: two tabs in the *same* browser already share one session id
(shared cookie), so behavior there is unchanged; two *separate* logins
by the same human (e.g. two different browsers, or one incognito) would
newly get independent selections instead of silently sharing one — which
matches this project's existing "no cross-session bleed" stance
(`docs/SESSION_STORE_PLAN.md`'s "Deliberately out of scope" section makes
the identical call for the auth session itself: independent Redis
entries per login, no shared-by-username semantics).

## The memory object

Key: `ui-memory:<session id>` — the **same** session id
`bff/session_store.py` mints at login (`ui-session:<id>`), but a
**separate** Redis key, not a field merged into that blob.

```json
{
  "pagination": {
    "query_id": "b6f17e2a-...",
    "filter": {"requester": "operator1", "review_type": null},
    "total": 42,
    "cached_at": 1755800000.123
  },
  "bulk_selection": ["req-abc123", "req-def456"]
}
```

- `pagination` is `null` until the session has listed anything.
- `bulk_selection` is a flat list, not split by role — a session's role
  is fixed at login (`require_session_role()` hard-gates which of
  `/ui/operator`/`/ui/manager` a session can even reach), so only one
  of "operator selection" / "manager selection" is ever meaningful for
  a given session; no need to carry both.
- No `role`/`username`/token fields here — identity lives in
  `ui-session:<id>` already; this blob is purely ephemeral UI state.

This JSON is what a `SessionMemory` class (see Phase 1) reads/writes —
callers never touch the raw dict/JSON shape directly.

**Why a separate Redis key from `ui-session:<id>`, not one merged
blob**: `ui-session:<id>` is written rarely (login, and access-token
refresh roughly every 5 minutes). `ui-memory:<id>` will be written far
more often (every bulk-select checkbox click; pagination writes are
throttled — see Phase 3 below, but still more frequent than a token
refresh). Neither `session_store.py` nor `SessionMemory` does locking —
every write is a full get-mutate-set of the whole blob. Merging both
concerns into one key means a token refresh landing between a
selection's read and its write would silently discard that refresh (or
vice versa) the next time either side writes back. Two independent keys
means these two concerns can never stomp on each other.

**TTL**: same sliding-on-every-touch policy as `ui-session:<id>`
(`SESSION_TTL_SECONDS = 1800`, reused from `session_store.py` rather
than a second constant) — this blob lives exactly as long as the login
does, and a period of inactivity clears it the same way it clears the
auth session. This is actually a *stronger* guarantee than today's
in-process `_bulk_selection`, which has no TTL at all and only clears on
an explicit fresh-page-load or confirmed bulk action.

**Where this lives, and why it's an asymmetry worth naming**:
`session_store.py` (the auth session — `ui-session:<id>`) lives in
`bff/`, since it's genuinely BFF-specific (Starlette `SessionMiddleware`
cookie handling, login/logout). This new module lives in `workflow/`
instead, per explicit direction — mirroring
`workflow/keycloak_auth.py`'s existing pattern of housing framework-
agnostic, reusable infrastructure (no FastAPI/Starlette imports; takes a
plain `redis.Redis` + session id string, not a `Request`) that `bff/`
wraps with the Starlette-specific plumbing (`bff/keycloak_session.py`
extracting `request.app.state.redis`/`request.session[SESSION_KEY]`
before calling into it, exactly as it already does for
`keycloak_auth.py`'s functions). Unlike `keycloak_auth.py`, `api/` has no
reason to ever import this module — REST API callers are stateless
bearer-token clients with no session — so this is reusable-by-position,
not reusable-in-practice today. That's an intentional, accepted
asymmetry with `session_store.py`'s placement, not an oversight; revisit
only if `api/` ever gains a genuine use for session-scoped memory.

## Phase 1 — `workflow/memory_service.py`, wired into logout, no behavior change

**Goal**: the module exists and is exercised by `logout()`'s cleanup,
but nothing yet reads or writes real pagination/selection data through
it — `bff/ui.py` keeps working exactly as today. Verifiable in isolation,
same shape as `docs/SESSION_STORE_PLAN.md`'s own Phase 1.

**Files**:
- `review_approval/workflow/memory_service.py` (new) — framework-
  agnostic (no FastAPI/Starlette imports; a plain `redis.Redis` +
  session id string in, never a `Request`), mirroring
  `workflow/keycloak_auth.py`'s placement. Two dataclasses plus the
  Redis interaction, all in one module (the "wrap around redis
  interaction logic and memory class definition" this phase asks for):

  ```python
  @dataclass
  class PaginationMemory:
      query_id: str
      filter: dict[str, str | None]
      total: int
      cached_at: float

      def is_stale(self, max_age_s: float) -> bool:
          return time.time() - self.cached_at >= max_age_s


  @dataclass
  class SessionMemory:
      pagination: PaginationMemory | None = None
      bulk_selection: list[str] = field(default_factory=list)

      # -- serialization --
      def to_json(self) -> str: ...
      @classmethod
      def from_json(cls, raw: str | bytes) -> "SessionMemory": ...

      # -- Redis I/O (the "wrap around redis interaction logic" half) --
      @classmethod
      async def load(cls, r: redis.Redis, session_id: str) -> "SessionMemory":
          """Never returns None -- an unknown/expired session id just
          yields a fresh, empty SessionMemory(), so callers never need a
          None-check before reading .bulk_selection/.pagination."""
          ...

      async def save(self, r: redis.Redis, session_id: str) -> None: ...

      @staticmethod
      async def delete(r: redis.Redis, session_id: str) -> None: ...

      # -- small mutation helpers, so bff/ui.py never touches the raw
      # dict/JSON shape directly --
      def select(self, request_id: str) -> None: ...
      def deselect(self, request_id: str) -> None: ...
      def clear_selection(self) -> None: ...
      def set_pagination(self, query_id: str, filter_: dict, total: int) -> None: ...
  ```

  `_KEY_PREFIX = "ui-memory:"`; `SESSION_TTL_SECONDS` **imported from
  `bff.session_store`, not redeclared** — the one deliberate exception
  to this module's "no `bff/` imports" framing, justified the same way
  `workflow/schemas.py` importing *within* `workflow/` is fine per
  `CLAUDE.md`'s dependency-direction rule: importing a constant is not
  the same as depending on `bff/`'s behavior, and redeclaring the same
  number twice is exactly how these two TTLs would silently drift apart
  after a future edit to one and not the other. If this asymmetry turns
  out to be uncomfortable in practice, the alternative is hoisting
  `SESSION_TTL_SECONDS` itself into `workflow/memory_service.py` and
  having `session_store.py` import it instead — a one-line fix, not a
  redesign, deferred until Phase 1 shows which direction reads better.
- `review_approval/bff/keycloak_session.py` — `logout()` also calls
  `memory_service.SessionMemory.delete(r, session_id)`, same
  best-effort try/except `RedisError` pattern already used for
  `session_store.delete()` there (a stray leftover entry is harmless,
  same reasoning as today).

**Tests**: a quick smoke check (`SessionMemory().save()` then `.load()`
round-trips including a populated `pagination`, `.load()` on an unknown
id returns a fresh empty `SessionMemory()` — not `None`, and not an
exception) — same convention as `docs/SESSION_STORE_PLAN.md`'s Phase 1.

## Phase 2 — Bulk selection moves to SessionMemory

**Goal**: `_bulk_selection` (the in-process dict) is gone; selection
survives a `bff` restart and works correctly across Kubernetes replicas.

**Files**:
- `review_approval/bff/ui.py`:
  - `_get_selection(username: str) -> set[str]` becomes `async def
    _get_selection(request: Request) -> set[str]`:
    ```python
    memory = await SessionMemory.load(request.app.state.redis, request.session[SESSION_KEY])
    return set(memory.bulk_selection)
    ```
    `_clear_selection(username: str)` becomes `async def
    _clear_selection(request: Request) -> None`: load, call
    `memory.clear_selection()`, `await memory.save(...)`. Every call
    site (~15, per the current grep — `operator_page()`,
    `operator_bulk_select()`, `bulk_decision_form()`,
    `bulk_decision_execute()`, and the manager-side mirrors of each)
    changes from `_get_selection(user["username"])` /
    `_clear_selection(user["username"])` to `await
    _get_selection(request)` / `await _clear_selection(request)` — a
    mechanical signature change, not a logic change; `request` is
    already in scope at every call site (it's a route handler parameter
    or passed down from one). This is exactly the seam the existing code
    comment at `_bulk_selection`'s definition already anticipated
    ("`_get_selection()`/`_clear_selection()` stay the seam, only their
    bodies would change") — delete that comment and the module-level
    dict along with it.
  - A per-row/select-all checkbox toggle (`operator_bulk_select()`/
    `manager_bulk_select()`) needs a small addition alongside its
    existing logic: `memory = await SessionMemory.load(...)`, call
    `memory.select(request_id)`/`memory.deselect(request_id)` per
    submitted id, `await memory.save(...)` — `pagination` is carried
    through untouched since it's just another field on the same loaded
    object, never read or reasoned about by this code path.
  - `RedisError` handling: same "fail closed, don't 500" instinct as
    `get_session_user()`, but the failure mode here is different — a
    Redis hiccup on a selection read should render the row/toolbar as
    *unchecked* (safe default, matches today's "a replica miss just
    shows unchecked, never wrong data" reasoning) rather than raising
    `RequireLoginRedirect` — a flaky Redis shouldn't log a user out just
    because they clicked a checkbox. `_get_selection()` catches
    `RedisError` from `SessionMemory.load()` and returns `set()`
    (equivalent to a fresh, empty memory); `_clear_selection()`/the
    select-toggle write path catch it around `.save()` and no-op (the
    click already happened client-side, per
    `docs/SELECT_ALL_CHECKBOX_PLAN.md`'s existing "own checked/unchecked
    state is already correct the instant it's clicked" design — a failed
    persist just means the *next* poll might not reflect it, not that
    this request should fail).

**Tests**: extend `tests/integration/test_bff_bulk.py` (which already
tests selection isolation between users) with a case that deletes the
session's `ui-memory:<id>` key directly mid-flow and confirms the
selection UI degrades to "nothing selected" rather than erroring — same
spirit as `test_bff_session_store.py`'s missing-entry test for the auth
session.

## Phase 3 — Pagination resilience fallback

**Goal**: `_resolve_operator_page()`/`_resolve_manager_page()` gain a
tier between "trust the client's `query_id`" and "recompute from
scratch," backed by Redis so it survives restarts and works across
replicas — without touching `workflow/service.py`'s `_query_cache` or
changing REST API behavior at all.

**Design, read side** (`_resolve_operator_page()`, mirrored for manager):
1. If the client supplied a `query_id`: try `service.list_reviews_page(
   pool, query_id=query_id, ...)` exactly as today (hits
   `workflow/service.py`'s existing in-process cache when available —
   cheapest path, unchanged, zero Redis traffic).
2. If that raised `ValueError` (unknown/expired — cache miss, possibly
   because this request landed on a different replica than the one that
   minted it): before falling all the way back to a fresh `COUNT(*)`,
   `memory = await SessionMemory.load(...)` and check `memory.pagination`.
   If it's non-`None`, its `filter.requester` matches this session's
   username (same ownership check `_resolve_operator_page()` already
   does today), and `not memory.pagination.is_stale(_QUERY_CACHE_TTL_S)`
   (imported from `workflow/service.py` rather than a second constant) —
   reuse its `filter`/`total` directly via a raw `PagedReviews`
   construction (no Postgres call at all).
3. Otherwise (no `query_id`, or session memory also stale/missing/
   mismatched): fall back to `service.list_reviews_page(pool,
   filter=...)` exactly as today — the existing, already-tested
   behavior, unchanged.

**Design, write side**: only when step 3 actually runs (a genuinely new
query is minted — matching the exact moment `workflow/service.py`'s own
`_cache_put()` would fire) does `_resolve_operator_page()` call
`memory.set_pagination(resolved_query_id, resolved_filter, total)` and
`await memory.save(...)`. **Deliberately not written on every call**
(i.e., not on every 5-second poll tick) — the poll's `query_id` is
already being supplied correctly by the client in the overwhelmingly
common case (step 1 hits), so writing session memory on every poll would
multiply Redis writes roughly 12x/minute/open-screen for zero benefit.
This keeps the hot path (open screen, healthy cache) exactly as cheap as
it is today — zero added Redis calls — and only pays the Redis cost on
the actually-rare paths (fresh navigation, filter change, or a genuine
cache miss).

**Files**: `review_approval/bff/ui.py` — `_resolve_operator_page()`,
`_resolve_manager_page()`, importing `SessionMemory` from
`workflow.memory_service` and `_QUERY_CACHE_TTL_S` from
`workflow.service` for reuse. `workflow/service.py` — **unchanged**.
`workflow/memory_service.py` — unchanged by this phase too (Phase 1
already built everything this phase needs; this phase is purely new
call sites in `bff/ui.py`).

**Tests**: a new case simulating a cross-replica miss — call
`_resolve_operator_page()` (or hit `/ui/operator/rows` with a
`query_id`) after directly clearing `workflow/service.py`'s
`_query_cache` (simulating "this landed on a different replica") but
with a valid, matching `ui-memory:<id>` entry present — assert the
resolved page's `total` matches the memory's cached value and no
`COUNT(*)` was re-run (e.g. by asserting on Postgres query count via a
lightweight instrumentation, or simply by asserting correctness under a
monkeypatched `_count_reviews` that fails the test if called).

## Phase 4 — Tests + docs

- Full existing suite (`tests/integration/test_bff_bulk.py`,
  `test_bff_pagination.py`) re-run to confirm no regression — both
  already exercise selection and pagination end to end, so a broken
  migration should surface immediately.
- `CLAUDE.md`: new bullet (mirroring the `workflow/keycloak_auth.py`
  bullet's framing — framework-agnostic, wrapped by `bff/`) describing
  `workflow/memory_service.py` and the `SessionMemory` class, and an
  update to `bff/ui.py`'s existing bullet removing references to the
  in-process `_bulk_selection` dict. This file's own status tracker
  flipped to "complete."

## Deliberately out of scope

- `workflow/service.py`'s `_query_cache` itself moving to Redis — it's
  shared REST-API/BFF infrastructure with no session/logout concept to
  hang a lifecycle off of; moving *it* to Redis (as a `query_id`-keyed,
  non-session-scoped cache, fixing the existing "different Kubernetes
  replica is a cache miss" gap noted in its own code comment) would be
  legitimate future work, but it's an orthogonal change to this plan and
  should be its own doc if pursued.
- Replacing client-side `page`/`query_id` round-tripping with pure
  server-remembered navigation (considered and explicitly rejected
  above — breaks multi-tab and browser back/forward).
- Any change to REST API behavior — `api/routes.py`'s
  `POST /reviews/search` is untouched by any phase here.
