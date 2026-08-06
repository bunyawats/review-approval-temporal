# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

A Review/Approval workflow system built on Temporal (Python SDK), with a
FastAPI BFF in front of it, PostgreSQL for queryable persistence, and
Keycloak for auth. Two roles: **Operator** (creates review requests),
**Manager** (approves/rejects them). Requests can also be edited or
cancelled by their own requester while still pending.

## Project structure

```
review-approval/
├── pyproject.toml           # single source of truth for deps + packaging
├── Dockerfile
├── docker-compose.yml
├── db/{schema.sql, init/}   # NOT part of the Python package -- consumed
│                             # directly by the postgres container via
│                             # bind mount, never imported by app code
└── review_approval/
    ├── __init__.py
    ├── app.py                # FastAPI app: lifespan, middleware, mounts
    │                         # bff.ui + bff.sandbox + api.routes
    ├── workflow/             # Temporal-side + shared business logic
    │   ├── __init__.py
    │   ├── workflows.py / activities.py / worker.py / task_queues.py
    │   └── service.py / schemas.py
    ├── api/                  # JSON REST surface (real Keycloak auth)
    │   ├── __init__.py
    │   └── routes.py / auth.py
    └── bff/                  # HTMX UI (mock auth) + htmx sandbox
        ├── __init__.py
        ├── ui.py / mock_auth.py / sandbox.py
        └── templates/*.html, templates/sandbox/*.html
```

Every module imports every other module by its full package path —
`from review_approval.workflow.workflows import ...`,
`from review_approval.workflow import service`, etc. No `sys.path`
manipulation anywhere. New modules go inside whichever of `workflow/`,
`api/`, or `bff/` they belong to (see "Architecture" below for how to
decide), never loose at the top level next to `app.py`.

Entrypoints (work from any directory once the package is installed):
- `python -m review_approval.workflow.worker`
- `uvicorn review_approval.app:app`

Dependencies are declared once, in `pyproject.toml`'s `dependencies` list.

## Architecture (do not violate these boundaries)

Three packages, one shared core:

- **`workflow/`** — Temporal-facing code, plus the business-logic layer
  both front doors call into. Nothing in here imports from `api/` or
  `bff/` — that dependency direction is one-way and load-bearing (see
  `task_queues.py` below for why).
- **`api/`** — the JSON REST surface. Imports from `workflow/`, never the
  reverse.
- **`bff/`** — the server-rendered HTMX UI (+ the htmx sandbox). Imports
  from `workflow/`, never the reverse.
- **`app.py`** — the only place that imports from all three, to assemble
  the actual FastAPI app and mount both front doors' routers.

New capability that both `api/` and `bff/` need → add it to
`workflow/service.py` first, then expose from both routers. New
capability that's REST-API-only or UI-only → add it directly in `api/`
or `bff/` respectively; don't put front-door-specific logic in
`workflow/`.

- **`workflow/workflows.py`** — `ReviewApprovalWorkflow`. Must stay
  **payload-agnostic**: it only ever sees `review_type: str` and
  `payload: dict[str, Any]`, and never inspects the dict's contents. All
  payload-shape validation belongs in `workflow/schemas.py`. Terminal
  states are `APPROVED`, `REJECTED`, `CANCELLED`, reached via the
  `submit_decision` and `cancel_request` signals; `_is_final()` guards
  every signal handler against acting twice. `update_payload` is only
  valid pre-decision.
- **`workflow/activities.py`** — the *only* file allowed to talk to
  Postgres. Workflow code never calls asyncpg directly; it goes through
  `workflow.execute_activity`. One activity per state-changing operation
  (`persist_request`, `persist_decision`, `persist_update`,
  `persist_cancel`) — don't collapse these into a single generic
  activity, since each has different column-update semantics.
- **`workflow/worker.py`** — long-running process that executes
  workflow/activity code, separate from the app. Any new activity must be
  registered in both `workflows.py`'s `imports_passed_through` block and
  `worker.py`'s activity list. Two independent env vars control it:
  - **`WORKER_MODE`** (`both` / `workflow` / `activity`, default `both`)
    — which half of the work this process registers. `workflow` mode
    never touches Postgres and needs no `DATABASE_URL`.
  - **`REVIEW_TYPE`** (unset, or one of `KNOWN_REVIEW_TYPES`) — which
    review-type-specific task queue(s) this process polls. Unset polls
    every known type (one `Worker` per type, concurrently, in this one
    process). Set polls only that type's queue — the mechanism for
    scaling one review type's worker capacity independently of others.
  Both axes compose freely and neither should be removed — Compose and
  the Kubernetes topology both depend on them.
- **`workflow/task_queues.py`** — single source of truth for
  `KNOWN_REVIEW_TYPES` and the task-queue naming scheme
  (`task_queue_for_review_type()`). Every review type gets its own
  Temporal task queue. This file has **zero dependency on `bff/` or
  `api/`** — `worker.py` imports it directly. `workflow/schemas.py`
  importing *from* here is fine (same package); `bff/` or `api/`
  importing from `workflow/` is fine (the established
  front-door-depends-on-core direction); the reverse must never happen.
- **`workflow/service.py`** — the *only* place that calls the Temporal
  client or reads/writes review data on behalf of a router. Both
  `api/routes.py` (JSON API) and `bff/ui.py` (HTMX UI) call into this
  module rather than duplicating business logic (ownership checks,
  status checks, payload validation). Add new capabilities here first,
  then expose from both routers.
  **`create_review`/`update_review`/`cancel_review`/`submit_decision`
  all wait for their write to actually land in Postgres before
  returning** (`_wait_until()`, a bounded poll: 50ms interval, 5s
  timeout, always returns whatever it last read even on timeout rather
  than raising). This exists because `client.start_workflow()` and
  `handle.signal()` only confirm Temporal *accepted* the start/signal —
  not that the workflow has run its handler or that the resulting
  `persist_*` activity (which runs asynchronously, whenever a worker
  process picks up the task) has actually committed. Without this, a
  caller that immediately re-queries Postgres afterward (every mutating
  route does, to re-render the list) can see stale pre-write data — on
  a local dev box with everything on localhost this race is usually won
  by luck, not guaranteed by anything the code actually enforces, and
  "usually" isn't good enough for a UI action whose whole point is
  showing the confirmed result. Verified: adds ~5-10ms in the normal
  case (confirmed via `time curl`), and degrades gracefully (waits the
  full 5s, then returns without raising) if the worker is down entirely
  — confirmed by killing the worker process mid-test; the row landed
  correctly once the worker came back, since the underlying Temporal
  operation was never abandoned, only the client-side wait for it gave
  up. Any new mutating capability added to this file needs the same
  treatment — don't return until the caller can actually see the effect.
- **`workflow/schemas.py`** — registry of Pydantic models keyed by
  `review_type`. Adding a review type touches two files: a model +
  registry entry here, and the type string added to `KNOWN_REVIEW_TYPES`
  in `task_queues.py`. An `assert` at the bottom of this file checks the
  two stay in sync and fails loudly at import time if not — don't remove
  it.
- **`api/routes.py`** — the JSON REST surface, real Keycloak auth. Thin:
  each route extracts params, calls `workflow/service.py`, maps
  exceptions to HTTP status codes.
- **`api/auth.py`** — Keycloak JWT validation + role-check dependency
  (`require_role("operator")` / `require_role("manager")`) for the JSON
  API. Temporal has zero concept of roles — this is the sole enforcement
  point for this front door. `KEYCLOAK_ISSUER` is read lazily, on first
  actual call to a protected route, not at import time, so the app
  (including `/ui/*`) runs fully without Keycloak configured at all.
- **`bff/mock_auth.py`** — session-cookie auth for the **POC UI only**
  (`bff/ui.py`). No password, no identity check — trusts whatever role
  the login form submitted. Never use for the JSON API. Swap-out path to
  real auth: replace what `login()` trusts with a Keycloak Authorization
  Code flow; keep storing `{"username", "role"}` in the session so
  `ui.py` doesn't need to change.
- **`bff/ui.py`** — server-rendered HTMX screens (`/ui/login`,
  `/ui/operator`, `/ui/manager`). Every route calls `workflow/service.py`,
  never Temporal/Postgres directly. Dialogs are HTML fragments swapped
  into `#dialog-container`; list refreshes use an out-of-band swap
  (`hx-swap-oob`) to close the dialog after a successful mutation — see
  the `clear_dialog` flag in `_operator_list.html`/`_manager_list.html`.
  Save/Cancel/Approve/Reject target **just their own row**
  (`#row-{request_id}`), not the whole `#request-list` — see
  `_operator_row.html`/`_manager_row.html` below. Every mutating route's
  *error* path still needs to land back in the dialog though, which no
  longer matches that row-scoped `hx-target` — those responses carry
  `_RETARGET_DIALOG_HEADERS` (`HX-Retarget: #dialog-container`,
  `HX-Reswap: innerHTML`), htmx's per-response override for exactly this
  case (confirmed present in the real v4 beta bundle, not just docs).
  Forgetting this on a new error branch means the error fragment tries
  to swap into a single `<tr>`, which is exactly the latent bug this
  pattern replaced — `create_request`'s two error branches had it
  (pre-existing, predates the row-targeting work, caught and fixed
  alongside it).
- **`bff/templates/`** — Jinja2 templates. `base.html` is the shell;
  `_*.html` are HTMX fragments, not full pages. This project's Starlette
  version requires **`TemplateResponse(request, name, context)`**
  (request first) — the older `TemplateResponse(name, {"request": ...})`
  form breaks here. Styling is Tailwind via a CDN script in
  `base.html` — no custom `<style>` blocks, no custom CSS classes; use
  Tailwind utility classes directly. Status-badge colors are a Jinja
  dict literal (`badge_classes`) **duplicated in three templates**
  (`_operator_list.html`, `_manager_list.html`, `_detail_dialog.html`) —
  a new status value needs all three updated. Templates ship as package
  data (`pyproject.toml`'s `[tool.setuptools.package-data]`, which uses a
  recursive `templates/**/*.html` glob so subdirectories like
  `templates/sandbox/` are included too); new `.html` files need no
  config change, other asset types would.
  **Deliberately tracks the latest major of both htmx and Tailwind** —
  `base.html` pins **htmx 4.x beta** (`https://unpkg.com/htmx.org@4.0.0-beta6`)
  and **Tailwind v4** via
  `https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4` — *not*
  `cdn.tailwindcss.com` (the old "Play CDN"), which only ever serves
  Tailwind v3 and was never updated for v4. `@tailwindcss/browser` is
  Tailwind's own v4-era replacement: same script-tag, no-build-step,
  scans-the-DOM-at-runtime behavior, just published under a different
  package. **htmx 4 is pre-release** (npm's `latest` dist-tag still
  points at 2.0.10; 4.x only exists under the `next` tag and is still
  moving between betas) — re-check this pin against
  `npm view htmx.org dist-tags` periodically and bump to a newer beta or
  the eventual stable release; don't assume beta6's behavior is final.
  When bumping either library, re-pin to the new exact version (don't
  leave it floating) and re-verify against the actual downloaded bundle,
  not just changelog prose — htmx 4's docs site describes some changes
  imprecisely (e.g. it renamed swap-related events to colon-separated
  names like `htmx:after:swap`, confirmed by grepping the real bundle
  for `htmx:` string literals, not by trusting the docs alone). Known
  v4 changes relevant here: `htmx.config.allowScriptTags` was removed
  as a *setting*, but the behavior it used to gate (creating a fresh
  `<script>` element to force execution of inline `<script>` tags in
  swapped content) is now unconditional — so `_form_dialog.html`'s
  inline `<script>` (the sample-payload auto-fill) still works, now
  without an escape hatch to disable it. `htmx.ajax(method, url, ctx)`
  keeps the same signature this codebase relies on (used directly in
  `_detail_dialog.html`'s Approve/Reject and `_operator_list.html`'s
  Cancel, bypassing `hx-vals` so a dismissed `prompt()` can abort the
  request instead of submitting an empty comment). All requests now go
  through `fetch()` instead of `XMLHttpRequest`; harmless here since no
  backend route inspects `HX-*` request headers. Tailwind v4 renamed
  the unsuffixed tier of a few scales (`shadow`→`shadow-sm`, old
  `shadow-sm`→`shadow-xs`, same pattern for `rounded`/`blur`) and
  changed the default `border` color from `gray-200` to `currentColor`.
  This codebase only uses suffixed variants (`rounded-md`, `rounded-xl`,
  etc.) and always pairs `border`/`border-b` with an explicit color
  class, so it isn't affected today, but a future utility-class addition
  could be — grep for bare `border`, `rounded`, `shadow`, or `blur` with
  no suffix before assuming it's still safe.
  **Two loading-indicator patterns, by whether an existing row can be
  targeted:**
  - **Save/Cancel/Approve/Reject** (an existing row is being mutated) —
    the spinner is embedded directly on the triggering button (matching
    `/sandbox/hx-indicator/`'s case 3), `hx-indicator` points at that
    spinner, and the request targets **`#row-{request_id}`** with plain
    `hx-swap="outerHTML"` — **no artificial `swap:Nms` delay**. This
    works because `workflow/service.py`'s `_wait_until()` (see that
    section below) already blocks the HTTP response until the
    `persist_*` activity has actually committed — the response *is* the
    confirmed final state by the time it arrives, so there's nothing
    left to fake with a delay, and the spinner can't disappear before
    the row updates because they're the same swap. A dialog closes (via
    its `hx-swap-oob="true"` div) in that *same* response, so dialog-
    close and row-update aren't two events to synchronize, just one.
  - **Create** (no row exists yet to target) — the only remaining
    exception, still using the older whole-`#request-list`-swap pattern:
    a persistent `#page-spinner` in `operator.html`'s header (outside
    anything that gets swapped/destroyed) plus `hx-swap="outerHTML
    swap:800ms"` on `_form_dialog.html`'s `<form>`, purely so the
    (already-confirmed-correct, thanks to `_wait_until()`) response has
    enough on-screen time to actually be perceived rather than flashing
    by. If a future change gives Create somewhere to target before the
    row exists (e.g. an optimistic placeholder row), drop this
    exception and fold it into the row-targeted pattern above.

  Every spinner (`#page-spinner`, and each per-button one) carries
  Tailwind's `transition-opacity duration-200` **unconditionally**, not
  gated behind `.htmx-request` — htmx's own injected CSS only animates
  the *show* direction (`.htmx-request .htmx-indicator{opacity:1;
  transition:opacity 200ms ease-in}`); the base `.htmx-indicator{opacity
  :0}` hide rule has no transition at all, so without this addition a
  spinner cuts off instantly rather than fading, which read as "it
  vanished before the content caught up" even when the underlying
  ordering was already correct — confirmed via a MutationObserver-
  instrumented reproduction at `/sandbox/hx-indicator/timing` that logs
  exact millisecond deltas instead of relying on how it looks. See the
  `htmx4` skill's "Loading indicators and swap timing" section for the
  general mechanism and the diagnostic-tooling lesson from building that
  reproduction (don't split one click's side effects across an inline
  `onclick=` and a separately-registered `addEventListener` when their
  relative firing order matters for what you're measuring — inline
  attribute handlers fire first).

  **History of this pattern, since it went through several iterations
  before landing here** (each already reverted, mentioned so the same
  ideas aren't re-tried expecting a different result): (1) spinner
  embedded on the Create button with the dialog staying open until the
  row appeared, dialog-close OOB synced to the same `swap:800ms` as the
  main target — reverted for a simpler, consistent-everywhere page-
  spinner version; (2) that consistent version, applied to all five
  actions uniformly via a whole-list `swap:800ms` — worked, but the
  *real* root cause of "spinner disappears before the row updates" for
  Save/Cancel/Approve/Reject turned out to be architectural, not
  visual: `client.start_workflow()`/`handle.signal()` only confirm
  Temporal *accepted* the operation, not that the `persist_*` activity
  (run asynchronously by a worker process) had committed — so the old
  whole-list swap could legitimately show pre-write data. Fixing that
  (via `_wait_until()`) made the row-targeted pattern above both
  possible and correct at the same time.
- **`bff/templates/_operator_row.html`, `_manager_row.html`** — each
  defines one Jinja `{% macro %}` rendering a single `<tr id="row-
  {request_id}">`. `_operator_list.html`/`_manager_list.html` `{% import
  %}` and call the macro once per row in their loop; `_operator_row_
  response.html`/`_manager_row_response.html` import and call the *same*
  macro to render just one row (plus an OOB dialog-close div when
  needed) for Save/Cancel/Approve/Reject's responses. One definition,
  two call sites — the full-list and single-row renderings can never
  drift apart on markup. Extending a row (new column, new action button)
  means editing the macro once, not both list templates.
- **`bff/sandbox.py`** — `/sandbox/*`, a standalone playground for htmx
  experiments, kept permanently alongside the app rather than thrown
  away after use (unlike an earlier throwaway `/ui/debug/*` diagnostic
  route, since deleted). No auth, no Temporal/Postgres — deliberately
  isolated so a mechanism can be verified on its own before touching
  real templates. Its templates extend `base.html`, so experiments run
  against the exact same htmx/Tailwind CDN pins as the real app rather
  than a separately hardcoded version. Own `Jinja2Templates` instance
  (not reused from `ui.py`) under `templates/sandbox/`, tagged
  `"Sandbox"` in the OpenAPI docs. Currently has two experiments:
  `hx-indicator` (`/sandbox/hx-indicator/`) — the four cases that
  proved out the loading-indicator pattern documented above and in the
  `htmx4` skill — and `hx-indicator/timing` (linked from that page) — a
  MutationObserver-instrumented reproduction of the real app's exact
  self-polling-target + external-indicator + delayed-swap structure,
  logging millisecond-precision deltas into an on-page `<pre>` instead
  of relying on how a transition looks. Add new experiments as
  additional routes in this file plus a `templates/sandbox/*.html`
  file, linked from `sandbox/index.html`.
- **`db/schema.sql`** — Postgres is the queryable/audit record; the JSON
  API's `GET /reviews` and the UI's list screens read from it directly
  (reads only — writes always go through a workflow signal/activity).
  Temporal is the source of truth for *live* workflow state. Not part of
  the `review_approval` Python package and never copied into the app's
  Docker image — only the `postgres` service in `docker-compose.yml`
  reads it, via bind mount.

## Invariants (not obvious from code alone — do not regress these)

- **Visibility**: Operators see only requests where
  `requester == their username`. Managers see *all* requests. Enforced
  by whether `service.list_reviews()` is called with a `requester`
  filter (operator routes) or without one (manager routes, JSON API's
  `GET /reviews`) — no separate permission check exists beyond that.
- **Terminal states are view-only, for both roles, no exceptions.** Once
  `APPROVED`/`REJECTED`/`CANCELLED`, nobody can edit, cancel, or
  re-decide — not the requester, not any manager.
  `update_review`/`cancel_review`/`submit_decision` in `service.py` all
  enforce this via `status != "PENDING_REVIEW"`; the UI mirrors it by
  only rendering action buttons on `PENDING_REVIEW` rows/dialogs. A new
  mutation needs the same guard in `service.py` — don't rely on the UI
  alone, since the JSON API is a second front door.
- **`review_type` is immutable after creation.** Only `payload` can be
  edited. The edit form disables the review-type `<input>` for this
  reason.
- **`closed_status`/`closed_by`/`closed_comment`/`closed_at` are shared
  across all three terminal outcomes** (`APPROVED`, `REJECTED`,
  `CANCELLED`) — not decision-specific. A cancellation populates all
  four the same as a decision does, just with the requester as the actor
  and `CANCELLED` as the status. `cancel_request`'s signal signature is
  `(self, cancelled_by: str, comment: str = "")`. A fourth terminal
  outcome, if ever added, should reuse these same four columns.
- Workflow ↔ activity data crosses via `@dataclass`, not raw dicts/tuples
  (see `PersistRequestInput`, `ReviewStatus`, etc.) — keep this pattern
  for any new activities.
- Workflow IDs are always `f"review-{request_id}"` (see `workflow_id()`
  in `workflow/service.py`). Don't introduce a second ID scheme.
- All four signals (`submit_decision`, `update_payload`,
  `cancel_request`) are idempotent/final-state-safe via the shared
  `_is_final()` guard.
- All activity calls use `start_to_close_timeout` + `RetryPolicy`.
- Ownership/status checks live in `workflow/service.py`, not in either
  front door. Both `api/routes.py` and `bff/ui.py` translate the same
  `LookupError` / `PermissionError` / `ValueError` exceptions from
  `service.py` into their own response format (HTTP status codes vs.
  re-rendered dialog fragments).

## Local dev: Docker Compose

`docker-compose.yml` + `Dockerfile` + `db/init/*.sh` are for **local dev
only** — not a preview of the Kubernetes shape below.

- One `Dockerfile`, shared by `bff`, `worker-workflow`, and
  `worker-activity` — same image (`pip install .` from `pyproject.toml`),
  different `command:`/`environment:` per service.
- `worker-workflow` and `worker-activity` are the same
  `python -m review_approval.workflow.worker`, split via `WORKER_MODE`.
  Both leave `REVIEW_TYPE` unset, so each polls every known review type's
  queue — keeps Compose simple rather than needing one service per
  review type. This split is for scaling/security isolation, not HA — HA
  comes from replica count
  (`docker compose up --scale worker-activity=3`).
- The `bff` service runs `uvicorn review_approval.app:app` — the service
  is still named `bff` in Compose (a deployment/infra naming choice,
  unrelated to the Python package layout) even though the app it runs
  now serves `bff/`, `api/`, and `/sandbox/*` together.
- One Postgres container hosts **two databases**: `temporal` (created
  automatically by the `temporalio/auto-setup` image) and
  `review_approval` (this app's data, created + schema-applied by
  `db/init/01-create-app-database.sh` and `02-apply-app-schema.sh` on
  first container start). Editing `db/schema.sql` doesn't affect existing
  containers — `docker compose down -v` to drop the volume and re-run
  init scripts.
- `KEYCLOAK_ISSUER` is deliberately unset in `docker-compose.yml` — the
  JSON API's protected routes 503 if called, `/ui/*` works fully without
  it. Don't add a Keycloak service without checking with the user first.

## Deployment target: Kubernetes

- **Deployments**: `bff` (serves the JSON API, `/ui/*`, and
  `/sandbox/*` together — not split into separate services) plus worker
  Deployments. Worker Deployments have two independent scaling axes,
  both via env vars on the same image/entrypoint:
  - `WORKER_MODE` (`workflow` / `activity`) — split by role.
  - `REVIEW_TYPE` (e.g. `purchase_order`) — split by review type, so a
    high-volume or slow-activity type can get its own Deployment and
    replica count without competing with others for the same worker
    pool.
  These compose freely, e.g. `worker-activity-purchase-order` (3
  replicas) next to `worker-activity-leave-request` (1 replica). For a
  small deployment, one `worker` Deployment with neither env var set
  (serves all types, both roles) is fine. All worker Deployments are
  stateless and horizontally scalable — HA comes from replica count, not
  from either split.
- **Every review type in `KNOWN_REVIEW_TYPES` needs at least one worker
  Deployment actually polling its queue** (`REVIEW_TYPE=<that type>`, or
  a catch-all with `REVIEW_TYPE` unset) — a workflow started on a queue
  nobody polls sits at `PENDING_REVIEW` forever with no error anywhere.
  There's no automated check for this.
- `TEMPORAL_HOST` must point at the in-cluster Temporal Service, not the
  `localhost:7233` default.
- `UI_SESSION_SECRET` **must** be a Kubernetes Secret shared across all
  `bff` replicas — the code's fallback value only works for a single
  local process.
- Postgres `max_connections` needs to account for `asyncpg` pool
  `max_size` × pod replica count across all Deployments running in
  `activity` or `both` mode (workflow-only Deployments never touch
  Postgres).
- The same `Dockerfile` used for Compose works as the K8s manifest base —
  no Compose-specific assumptions baked in.

## Known gaps

- No timeout on "wait for Manager decision" — requests can wait forever.
- No notification activity (email/Slack) on request creation or decision.
- `verify_aud=False` in `api/auth.py` — needs a real audience once the
  Keycloak client is finalized.
- No automated check that every `KNOWN_REVIEW_TYPES` entry has a worker
  polling its queue.
- `bff/mock_auth.py` / `/ui/*` have no real authentication — see its
  docstring before extending or deploying it anywhere shared.
- No test suite yet.

## Running locally

Preferred: `docker compose up --build` (see README.md's Setup section) —
no env vars needed, `docker-compose.yml` sets them all.

Native alternative:

```bash
temporal server start-dev                                    # separate terminal
python -m review_approval.workflow.worker                    # separate terminal
uvicorn review_approval.app:app --reload --port 8000          # separate terminal
```

Requires `pip install -e .` first, and `DATABASE_URL` set (plus
`KEYCLOAK_ISSUER` only if calling the JSON API) — see `.env.example`.

## Testing changes

There's no test suite yet. When adding one, prefer Temporal's
`temporalio.testing.WorkflowEnvironment` (time-skipping test environment)
for workflow/activity tests over hitting a real Temporal server. Tests go
under a new `tests/` directory at the repo root, not inside the package.
