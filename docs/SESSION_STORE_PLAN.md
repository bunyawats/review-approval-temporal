# Server-side session store (Redis) for `/ui/*` + real token refresh

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (same convention as `docs/PAGINATION_PLAN.md` and
> `keycloak/INTEGRATION_PLAN.md`).
>
> - [x] Phase 1 — Redis infra (Docker service, `bff/session_store.py`,
>       `app.state.redis` lifespan wiring), no behavior change yet
> - [x] Phase 2 — `refresh_access_token()` in `workflow/keycloak_auth.py`,
>       `bff/keycloak_session.py` rewritten to store tokens in Redis
>       (browser cookie holds only an opaque session id) and transparently
>       refresh on access-token expiry
> - [x] Phase 3 — tests (missing-Redis-entry forces re-login, refresh
>       actually happens and is transparent to the caller), docs
>       (`CLAUDE.md`, `README.md`) updated to describe the final shape
>
> **Implementation notes, beyond the original sketch above:**
> - `auth_callback()` in `bff/ui.py` used to read
>   `request.session["user"]["role"]` directly after `complete_login()` --
>   a raw session-dict access this plan's Phase 2 file list didn't catch
>   (it only called for auditing direct calls to `get_session_user()`/
>   `logout()`, not raw `request.session[...]` reads). Under the new shape
>   `request.session["user"]` is an opaque id string, so this would have
>   thrown `TypeError` on every successful login. Fixed by having
>   `complete_login()` return the role directly instead.
> - Two failure modes not originally specified here, decided during
>   implementation: `get_session_user()` catches `redis.exceptions.RedisError`
>   and treats a Redis outage as "please log in again" rather than a raw
>   500 (fail-closed, matching this codebase's existing degrade-gracefully
>   pattern elsewhere). A Keycloak-unreachable failure specifically during
>   a refresh call is deliberately *not* folded into `RefreshFailed` --
>   `refresh_access_token()` lets a network-level `httpx` error propagate
>   raw rather than silently logging out every active session on a
>   transient blip (mirrors `get_permissions()`'s own
>   `PermissionCheckError` vs. `TokenInvalid` split).

## Context

This came out of a security brainstorm session (see chat history, not
reproduced here) that started from a proposal to move `query_id`/`page`
(the pagination cache key and current page number — see
`docs/PAGINATION_PLAN.md`) into a Redis-backed server-side session, on
the reasoning that they shouldn't cross the network even though they
aren't shown in the UI itself.

That specific concern turned out to be low-stakes: `query_id` is a
random opaque cache-lookup key with no access rights of its own, and
`workflow/service.py`'s `list_reviews_page()` cache-lookup path already
re-validates that a cached entry's `filter.requester` matches the
current session before trusting it (see `docs/PAGINATION_PLAN.md`'s BFF
note and `bff/ui.py`'s `operator_list()`) — so even a leaked `query_id`
can't be used to read another operator's requests. `requester` itself
is never actually sent to the client in the pagination flow at all; it's
derived from the session server-side.

**The real, higher-value problem found along the way**: `bff/
keycloak_session.py`'s session is stored entirely in Starlette's
`SessionMiddleware` cookie — *signed* (can't be forged) but **not
encrypted**. The cookie currently holds the raw Keycloak `access_token`,
readable in plaintext by anything with access to that cookie (browser
dev tools on a shared/compromised machine, a misconfigured proxy log,
etc.) — a much more concrete exposure than pagination state ever was.
`access_token`/`refresh_token`/`id_token` together were already measured
at ~4.5KB signed during the original Keycloak integration work, over the
~4KB limit real browsers enforce per cookie (see that module's
docstring) — which is *why* `refresh_token`/`id_token` were dropped
from the cookie in the first place, and *why* there's no token refresh
today: without a stored `refresh_token`, there's nothing to exchange for
a new `access_token`, so a 5-minute-old session just forces re-login.

A server-side store removes both problems at once: the browser only
ever holds an opaque session id (nothing sensitive to read), and the
store itself — with no 4KB constraint — can hold `refresh_token` too,
making real token refresh possible. This plan targets **that** problem,
not the pagination one.

**Live Keycloak realm settings, confirmed via the Admin REST API against
the running `myrealm` realm** (not assumed from the import JSON, which
sets none of these explicitly — see below):

| Setting | Value |
|---|---|
| `accessTokenLifespan` | 300s (5 min) |
| `ssoSessionIdleTimeout` | **1800s (30 min)** |
| `ssoSessionMaxLifespan` | 36000s (10 hr) |
| `revokeRefreshToken` | `false` — refresh tokens are **not** rotated/single-use |
| `review-approval` client attributes | no per-client overrides — inherits all of the above |

Two things fall out of this:
- **The 30-minute target is already exactly what Keycloak allows** —
  `ssoSessionIdleTimeout=1800s` means a refresh token stays exchangeable
  for up to 30 minutes of inactivity. No Keycloak-side config change
  needed.
- **`revokeRefreshToken=false` simplifies the refresh implementation** —
  the same refresh token can be reused across multiple refreshes; no
  need to persist a rotated token after every renewal (though the code
  should still store whatever Keycloak returns, in case this setting
  ever changes).

`keycloak/import/myrealm-realm.json` doesn't set any of the above keys
explicitly, meaning the realm currently relies on Keycloak's own
built-in defaults for a 26.0 server. If this plan is implemented on a
different Keycloak version or a realm-import edit ever sets these
explicitly, **re-verify live via the Admin API** (see the curl recipe in
chat history / re-derive via `GET
/admin/realms/myrealm`) rather than trusting this table blindly — it's
a snapshot, not a guarantee.

## Design

**Session shape moves from** (today, all in the cookie):
```
{"username", "role", "access_token", "expires_at"}
```
**to** (cookie holds only a session id; Redis holds the rest):
```
cookie:  request.session["user"] = "<opaque session id>"
redis:   ui-session:<id> -> {
           "username", "role",
           "access_token", "access_expires_at",
           "refresh_token", "refresh_expires_at",
         }
```

The pre-login OAuth CSRF `state` value (`build_authorize_url()`'s
`_STATE_KEY`) stays in the signed cookie as-is — it's tiny, short-lived
(discarded in `complete_login()`), and never holds anything sensitive
enough to need a server-side store.

**TTL is sliding, not a hard cap**: every successful session lookup
refreshes the Redis key's expiry (`EXPIRE ui-session:<id> 1800`), so
"session lasts 30 minutes" means 30 minutes of *inactivity*, matching
`ssoSessionIdleTimeout` — not a fixed 30 minutes from login regardless
of use. (`ssoSessionMaxLifespan`'s 10-hour ceiling is a Keycloak-side
backstop this plan doesn't need to separately enforce — a refresh will
naturally start failing once the underlying Keycloak SSO session hits
it.)

**Refresh happens lazily, in `get_session_user()`** (the single choke
point already called by both `require_session_role()` and
`require_permission()`): if the stored `access_token` is expired (or
within some small buffer of expiring), exchange the stored
`refresh_token` for a new one via Keycloak's `grant_type=refresh_token`,
update the Redis entry, and continue — transparent to the caller. Only
if the refresh itself fails (refresh token expired past the 30-min idle
window, or revoked) does this fall back to today's behavior:
clear the session, raise `RequireLoginRedirect`.

**"Redis has no matching entry" forces re-login for free** — this was
the original ask, and it falls out of the design directly: if the
session-id cookie is present but `session_store.get()` returns `None`
(evicted, expired via Redis's own TTL, Redis restarted with no
persistence, or never existed), `get_session_user()` treats it exactly
like today's "invalid/missing session cookie" case.

## Phase 1 — Redis infrastructure, no behavior change

**Goal**: Redis is up (Compose + the native hybrid setup) and reachable
from the app, with a thin store module, but nothing yet reads or writes
real session data through it — `bff/keycloak_session.py` keeps working
exactly as today. Verifiable in isolation before any auth-flow risk.

**Files**:
- `docker-compose.yml` — new `redis` service (`redis:7-alpine`, no
  volume — losing data on restart just forces re-login, which is
  already this design's intended fallback, not a regression to guard
  against). `bff` gets `REDIS_URL=redis://redis:6379/0` and a
  `depends_on: redis`.
- `.env.example` — `REDIS_URL=redis://localhost:6379/0` (matches the
  native-hybrid pattern already used for `KEYCLOAK_ISSUER` — Homebrew's
  `redis-server`/`redis-cli` are already installed locally, or `docker
  compose up -d redis` alone works the same way `keycloak` does today).
- `pyproject.toml` — add `redis>=5.0.0` (redis-py; async support lives
  at `redis.asyncio`, no separate package needed).
- `review_approval/bff/session_store.py` (new) — thin wrapper: `get(r,
  session_id) -> dict | None` (also slides the TTL forward on a hit),
  `set(r, session_id, data)`, `delete(r, session_id)`,
  `new_session_id()` (`secrets.token_urlsafe`, matching the existing
  OAuth `state` generation style in `keycloak_session.py`). JSON-encoded
  values, `ui-session:` key prefix, `SESSION_TTL_SECONDS = 1800`.
- `review_approval/app.py` — lifespan connects `redis.asyncio.from_url(os.environ["REDIS_URL"])`
  into `app.state.redis`, closes it (`aclose()`) on shutdown, alongside
  the existing Temporal client / Postgres pool setup.

**Tests**: none behavior-visible yet (nothing calls `session_store` from
real request handling in this phase) — a quick smoke check
(`session_store.set()` then `get()` round-trips, `get()` on an unknown
id returns `None`) is enough to confirm the module works before Phase 2
wires it into the real login flow.

## Phase 2 — Wire it into the real session + add refresh

**Goal**: `/ui/*` sessions are Redis-backed end to end, access tokens
refresh transparently, and the browser cookie only ever holds an opaque
id. This is the phase that actually changes auth behavior.

**Files**:
- `review_approval/workflow/keycloak_auth.py` — new
  `refresh_access_token(refresh_token: str) -> dict` (POSTs
  `grant_type=refresh_token` to Keycloak's token endpoint, returns the
  raw token response — `access_token`, `refresh_token`, `expires_in`,
  `refresh_expires_in`); new `RefreshFailed` exception (refresh token
  itself expired/revoked/invalid — Keycloak's `invalid_grant` response),
  mirroring the existing `TokenInvalid`/`PermissionCheckError` style in
  this file.
- `review_approval/bff/keycloak_session.py` — the real rewrite:
  - `complete_login()`: after the code exchange, store the *full* token
    set (`access_token`, `refresh_token`, both expiries, `username`,
    `role`) in Redis via `session_store.set()` under a freshly minted
    `session_store.new_session_id()`; the cookie gets only that id.
  - `get_session_user()` becomes `async`: look up Redis by the cookie's
    session id; `None` → `RequireLoginRedirect` (covers both "never
    existed" and "TTL expired"); if the stored `access_token` is expired,
    call `refresh_access_token()`, persist the updated tokens back to
    Redis, and continue; `RefreshFailed` → clear the Redis entry, raise
    `RequireLoginRedirect`, same as today's "expired, no refresh"
    fallback.
  - `require_session_role()`'s inner `checker` becomes `async def` (it
    just `await`s the now-async `get_session_user()`); `require_permission()`
    already awaits things, no shape change needed there beyond whatever
    falls out of `get_session_user()` becoming async.
  - `logout()` becomes `async def`: delete the Redis entry (via
    `session_store.delete()`) in addition to popping the cookie key —
    today it only does the latter, which would leave an orphaned Redis
    entry (harmless — it just expires on its own TTL — but not
    cleaned up promptly).
  - `check_permission()`'s existing `TokenInvalid` → `RequireLoginRedirect`
    handling stays as a last-resort path: by the time it runs, whatever
    called `get_session_user()` first should already have a fresh
    `access_token`, so this only fires if Keycloak rejects a token for
    some other reason (e.g. revoked out-of-band).
- `review_approval/bff/ui.py` — `logout_submit()`'s `logout(request)`
  call needs `await` now that `logout()` is async. Grep for any other
  direct (non-`Depends()`) call site of `get_session_user()`/`logout()`
  before finishing this phase — there may not be any beyond what's
  listed here, confirm rather than assume.

**Tests**: none new required by this phase in isolation — Phase 3 covers
the behavior this phase introduces. Do run the *existing* full suite
(`tests/integration/test_bff_login.py`, `test_bff_permissions.py`,
`test_bff_pagination.py`) after this phase, since they all drive real
logged-in sessions through the real login flow and would immediately
surface a broken rewrite.

## Phase 3 — Tests + docs

**Goal**: the two behaviors this plan exists for are locked in by tests,
and the docs describe the final shape rather than the old one.

**Tests** (new file, `tests/integration/test_bff_session_store.py`,
following the existing integration-test conventions — duplicated login
helpers, `pytest.mark.integration`):
- Logging in, then deleting the session's Redis key directly (simulating
  eviction/restart), then hitting any session-gated route → redirects to
  `/ui/login` (303), not a 500 or a silently-still-logged-in response.
- Refresh actually happens and is transparent: log in, then directly
  overwrite the stored `access_expires_at` in Redis to something already
  past (rather than waiting 5 real minutes), then hit a session-gated
  route → succeeds (200, not a redirect to login), and the Redis entry's
  `access_token` afterward differs from the pre-refresh one (proves a
  real refresh call happened, not that the old token was reused past its
  claimed expiry).
- Logout actually clears the Redis entry (not just the cookie) — log in,
  capture the session id, log out, confirm `session_store.get()` for
  that id returns `None`.
- (Optional, lower priority) A refresh with a genuinely invalid/expired
  `refresh_token` falls back to `RequireLoginRedirect` rather than
  raising an unhandled exception — may need a way to force Keycloak to
  actually reject the refresh (e.g. an artificially corrupted stored
  token) rather than relying on real 30-minute idle expiry in a test.

**Docs**:
- `CLAUDE.md` — `bff/keycloak_session.py`'s bullet needs a real rewrite:
  the "session shape" line, the "deliberately NOT storing
  refresh_token/id_token" reasoning (now storing both, server-side, so
  the cookie-size constraint that drove that decision no longer applies
  to them — it still applies to the cookie itself, which is why the
  cookie holds only an id), and the "No access-token refresh in
  bff/keycloak_session.py" bullet under "Known gaps" (now resolved,
  should move out of that section entirely, similar to how the
  pagination gap bullet was rewritten from "planned" to "complete" in
  `docs/PAGINATION_PLAN.md`'s Phase 3).
- `README.md` — "Running locally" needs a Redis line for both Compose
  and the native/hybrid path; note that `/ui/*` login now depends on
  Redis being reachable (JSON API routes don't — they're independent of
  `bff/keycloak_session.py` entirely).
- Sweep for stale references to "no token refresh" / "session stored in
  the cookie" elsewhere in comments/docstrings (this file's own listing
  above is the starting point, not necessarily exhaustive).

## Deliberately out of scope for this plan

- Moving `query_id`/`page` (pagination state) into this same session
  store — the brainstorm that led here concluded this isn't a real
  exposure (see "Context" above); revisit only if that reasoning turns
  out to be wrong.
- Multi-session-per-user semantics (this plan keys Redis by a random
  session id generated at login, one entry per login — *not* by
  Keycloak `sub`/username, so multiple concurrent logins for the same
  user, e.g. two browser tabs or devices, naturally get independent
  Redis entries and don't collide or kick each other out; no
  "log out everywhere" mechanism is being built, since nothing requires
  enumerating a user's other sessions to reach that outcome).
- `ssoSessionMaxLifespan`'s 10-hour absolute ceiling isn't separately
  enforced by this app — Keycloak already enforces it on its own end;
  this app's refresh call will simply start failing once that's hit.