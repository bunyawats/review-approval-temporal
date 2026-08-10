# Full Keycloak integration: real login, real permission checks, tests

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (no dependency on `~/.claude/plans/`, which isn't part of this
> repo and isn't guaranteed to survive across machines/sessions).
>
> - [x] Phase 1 — shared auth core (`workflow/keycloak_auth.py`)
> - [x] Phase 1 — `api/auth.py` (`require_permission()`, `check_permission()`)
> - [x] Phase 1 — `api/routes.py` wired to real permissions, verified
>       live against real Keycloak (create/approve/create-denied all
>       confirmed correct)
> - [x] Phase 1 — unit tests (`tests/unit/test_keycloak_auth.py`, 11
>       tests, no live services needed, ~0.3s)
> - [x] Phase 1 — integration tests (`tests/integration/test_api_permissions.py`,
>       11 tests against the real local stack, all passing) —
>       **Phase 1 complete.**
> - [x] Phase 2 — `bff/keycloak_session.py` (Authorization Code flow,
>       replaces `bff/mock_auth.py` -- **already deleted**, not deferred
>       to Phase 4), real login/callback/logout routes in `bff/ui.py`,
>       `login.html` updated, realm's `redirectUris`/`post.logout.
>       redirect.uris` tightened from `["*"]` to real URLs
> - [x] Phase 2 — integration tests (`tests/integration/test_bff_login.py`,
>       4 tests: operator login, manager login, wrong password rejected,
>       logout) — verified against both `TestClient` and the real native
>       app. **Phase 2 complete.** Two real bugs caught only by testing
>       this properly instead of eyeballing the code, both written up in
>       the `keycloak-admin` skill: cookie-size (dropped
>       refresh_token/id_token from the session -- all three JWTs
>       together were ~4.5KB signed, over what real browsers accept per
>       cookie) and `post.logout.redirect.uris` needing to be set
>       explicitly (a wrong assumption that `redirectUris` alone would
>       cover it, and that Keycloak's documented `"+"` shorthand would
>       work -- it didn't, in this version).
> - [ ] Phase 3 — BFF permission enforcement (replace
>       `require_session_role()`'s role bridge with real
>       `get_permissions()` checks; button visibility to match)
> - [ ] Phase 4 — cleanup (mock_auth.py already gone; update
>       CLAUDE.md/README.md for Phase 3's changes once it lands)

## Context

The Keycloak side is done and verified (realm, roles, Resources/Policies/
Permissions, 4 demo users — `keycloak/import/myrealm-realm.json`,
`docker-compose.yml`'s `keycloak` service). The application side never
caught up:

- `api/auth.py` still checks `require_role("operator")`/`require_role("manager")`
  against the plain JWT's `realm_access.roles` claim — a stale check
  that can't even see the real permissions, since `Create_Request` etc.
  are Resources now, not roles (confirmed live: valid tokens get `403
  requires role: operator`, a wrong check, not an auth failure).
- `bff/mock_auth.py` is still fully mock — no password, trusts whatever
  role a session cookie claims. Its own docstring already names the
  swap-out path: replace `login()`'s "trust the submitted role" with a
  real Keycloak Authorization Code flow.
- There is no `tests/` directory at all yet (`pyproject.toml` already
  declares `pytest`/`pytest-asyncio` as dev deps, unused so far).

This plan closes all of that: real login for the HTMX UI, real
permission enforcement (via the UMA ticket exchange already verified
working by hand this session) on every route in both front doors, and a
real test suite. It's staged as **4 independent, each-independently-
mergeable phases**, meant to be done one session at a time — a session
picking this up mid-plan should read this file, check which phase's
files already exist/are wired up, and resume from there.

**Ordering rationale**: the user's four asks (login flow / permission
control / endpoint enforcement / tests) don't have to land in that exact
order to be complete — permission control is a *dependency* of the
other three, so it goes first, applied to the REST API (which already
has *some* real Keycloak plumbing via bearer tokens — no redirect flow
to build) before the BFF (which needs a real login flow *and* real
permission checks, the most complex remaining piece, tackled last with
the core already proven).

## Shared architecture decision (confirmed with user)

A new module, **`review_approval/workflow/keycloak_auth.py`**, holds the
low-level Keycloak logic both front doors need: JWT decode/validation,
and the UMA ticket exchange for permission checks. `workflow/` is
already documented as "the shared core both front doors call into," and
both `api/` and `bff/` already import from it one-way — this fits that
existing boundary exactly, without a new top-level package or a
bff-imports-from-api (or vice versa) edge that doesn't exist today.

This module exposes two things, not just a `Depends()` factory, because
`POST /reviews/{id}/decision` (REST) and its BFF equivalent
(`manager_decision`) need to check **one of two different permissions**
depending on the submitted `decision` value (`APPROVED` →
`Approve_Request`, `REJECTED` → `Reject_Request`) — something a single
FastAPI dependency can't express:

- `async def get_permissions(access_token: str) -> set[str]` — does the
  UMA ticket exchange (`grant_type=urn:ietf:params:oauth:grant-type:
  uma-ticket`, `audience=review-approval`, resource server's own
  `client_id`/`client_secret`), returns the set of granted Resource
  names. Callable directly inside a route body for the decision-branch
  case.
- `async def decode_token(token: str) -> dict` — the JWT signature/
  issuer validation logic, moved out of `api/auth.py` essentially as-is
  (same `PyJWKClient` approach, same `verify_aud=False` gap carried
  forward unchanged — out of scope for this plan).

No caching in the first pass — every permission check is a live UMA
call. Note this as a known latency cost (not fixed here); if it matters
later, the natural fix is caching the RPT for its own validity window.

New env vars needed (add to `.env.example`): `KEYCLOAK_CLIENT_ID`
(`review-approval`), `KEYCLOAK_CLIENT_SECRET` (`dev-secret-change-me`
locally, matching `keycloak/import/myrealm-realm.json`). New runtime
dependency: `httpx` (async, matches FastAPI's own async style — check
if already transitively present before adding explicitly). New dev
dependency: `respx` (httpx call mocking, for fast unit tests without a
live Keycloak).

---

## Phase 1 — Shared auth core + REST API enforcement + tests

**Goal**: `api/routes.py` genuinely enforces the 5 permissions via
Keycloak, verified by both mocked unit tests and real-Keycloak
integration tests. This alone closes the "Known gaps" item from
`CLAUDE.md`.

**Files**:
- New `review_approval/workflow/keycloak_auth.py` (per above)
- `review_approval/api/auth.py` — replace `require_role(role: str)` with
  `require_permission(permission: str)`, a `Depends()` factory built on
  `keycloak_auth.get_permissions()`; keep `get_current_user()` (used by
  `GET /reviews`/`GET /reviews/{id}`, which stay identity-only, no
  permission gate, per existing behavior)
- `review_approval/api/routes.py` — swap each route's dependency:
  `create_review` → `Create_Request`, `update_review` → `Update_Request`,
  `cancel_review` → `Cancel_Request`, `submit_decision` → check
  `Approve_Request`/`Reject_Request` inside the handler based on
  `body.decision` (raise `HTTPException(403)` directly, matching this
  file's existing exception-mapping style)
- `.env.example`, `pyproject.toml` (new deps)

**Tests** (new `tests/` dir at repo root, per `CLAUDE.md`'s existing
guidance):
- `tests/unit/test_keycloak_auth.py` — `respx`-mocked: token validation
  (valid, expired, bad signature, wrong issuer), permission check
  (granted/denied/Keycloak-unreachable)
- `tests/integration/test_api_permissions.py` — against the **real**
  local Keycloak + Postgres + Temporal (a pytest fixture logs in each of
  the 4 demo users via the real password grant, matching
  `README.md`'s "Try it" curl example), hitting all 5 routes with all 4
  users via `httpx.AsyncClient` against the real app, asserting the
  correct 200/403 per user/route combination — this is the automated
  version of the manual curl verification already done by hand this
  session. Mark with `@pytest.mark.integration` (or similar) so it's
  skippable when the local stack isn't up.

**Verification**: `pytest tests/unit` runs with no services running;
`pytest tests/integration` requires `docker compose up -d keycloak` +
native Postgres/Temporal/worker up, reproduces (now automated) the
`403 requires role: operator` → fixed → `200` transition confirmed by
hand earlier this session.

---

## Phase 2 — Real login flow for the BFF (Authorization Code)

**Goal**: `/ui/login` authenticates against real Keycloak instead of
trusting a submitted role.

**Files**:
- `review_approval/bff/mock_auth.py` → replaced (name TBD at
  implementation time, e.g. `bff/keycloak_session.py`) — `login()`
  becomes "redirect to Keycloak's `/protocol/openid-connect/auth`";
  new `GET /ui/callback` route exchanges the returned `code` for tokens
  via `{issuer}/protocol/openid-connect/token` (`grant_type=
  authorization_code`, needs `client_secret` — confidential client);
  session stores `{"username", "access_token", "refresh_token",
  "expires_at"}` (not just `{"username","role"}` — no more single
  `role` string once permissions are the model, and a real token is
  needed for Phase 3's checks)
- `logout()` should also redirect through Keycloak's own
  `/protocol/openid-connect/logout` for a real single-logout, not just
  clear the local cookie
- `keycloak/import/myrealm-realm.json` — tighten `review-approval`'s
  `redirectUris`/`webOrigins` from the current dev-permissive `["*"]` to
  the actual callback URL(s) now that they're known
- `login.html` — replace the 4 one-click demo buttons with a single
  "Log in with Keycloak" link (demo users still exist in Keycloak, just
  authenticate for real now — same 4 usernames/`password` still work)

**Open item to resolve at implementation time, not blocking this plan**:
token refresh — access tokens expire in 5 minutes (confirmed this
session); decide then whether to refresh transparently on each request
or force re-login on expiry (simpler, reasonable for a POC).

**Tests**: `tests/integration/test_bff_login.py` — drive the real
redirect/callback exchange against the real local Keycloak (no browser
needed; Keycloak's login form can be POSTed to directly with `httpx`,
same technique as the password-grant curl calls already verified this
session) for at least one demo user, asserting a valid session results.

---

## Phase 3 — Permission-based authorization on BFF routes

**Goal**: `bff/ui.py` enforces the same 5 permissions as the REST API,
via the same `workflow/keycloak_auth.py` core.

**Files**:
- `review_approval/bff/ui.py` — replace `require_session_role("operator")`/
  `require_session_role("manager")` throughout with a BFF-flavored
  permission dependency (redirects to login / renders an error fragment
  on denial, instead of a bare 403 — matching this file's existing
  error-handling conventions); `manager_decision` gets the same
  decision-value-branching treatment as the REST API's `submit_decision`
- Templates (`_operator_row.html`, `_manager_row.html`, etc.) — Edit/
  Cancel/Approve/Reject buttons should reflect the logged-in user's
  *actual* granted permissions (fetched once per page render), not just
  the coarse pending/non-pending status check they use today — defense
  in depth, matching the existing pattern of UI mirroring server-side
  enforcement rather than being the only guard

**Tests**: `tests/integration/test_bff_permissions.py` — same
per-route/per-user matrix as Phase 1's API tests, but against `/ui/*`
routes with real logged-in sessions from Phase 2's flow.

---

## Phase 4 — Cleanup

**Goal**: no leftover mock/dead code, docs match reality.

- Delete the old `bff/mock_auth.py` if renamed rather than edited in
  place in Phase 2
- `CLAUDE.md`/`README.md` — update the `api/auth.py`/`bff/mock_auth.py`
  bullets and the "Known gaps" entry (this whole effort resolves it) to
  describe the real, implemented state — same pattern already used
  throughout this project's docs (describe reality precisely, flag
  what's still a known limitation, e.g. no token-refresh, no `aud`
  verification)
- Sweep for any remaining reference to the old role-based model
  (`require_role`, `require_session_role`, `VALID_ROLES`) to confirm
  nothing was missed
