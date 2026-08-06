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
  The Create/Save form's loading indicator (`operator.html`'s
  `#page-spinner`, wired via `_form_dialog.html`'s
  `hx-indicator="#page-spinner"` and `hx-swap="outerHTML swap:800ms"`)
  deliberately lives in `operator.html`'s persistent header, **not**
  inside the dialog or inside `#request-list` — both of those get
  destroyed/replaced as part of a successful submit, so an indicator
  nested in either only has a window to be seen for as long as content
  survives, which combined with how fast a local Create actually
  completes can be effectively zero. The dialog itself closes
  immediately (its `hx-swap-oob="true"` div has no delay); only the
  main list swap carries the `swap:800ms` modifier — this is
  intentional, not an inconsistency: it lets the dialog close right
  away while the persistent-page spinner keeps spinning until the
  (now visually decoupled) list actually updates. The same pattern
  (`hx-indicator` + `source: this` + `swap: 'outerHTML swap:800ms'`)
  is applied to `_detail_dialog.html`'s Approve/Reject and
  `_operator_list.html`'s Cancel too, each pointing at its own page's
  `#page-spinner` (`operator.html`'s or `manager.html`'s). See the
  `htmx4` skill's "Loading indicators and swap timing" section for the
  general pattern and the OOB-swap-timing gotcha this depends on.
- **`bff/sandbox.py`** — `/sandbox/*`, a standalone playground for htmx
  experiments, kept permanently alongside the app rather than thrown
  away after use (unlike an earlier throwaway `/ui/debug/*` diagnostic
  route, since deleted). No auth, no Temporal/Postgres — deliberately
  isolated so a mechanism can be verified on its own before touching
  real templates. Its templates extend `base.html`, so experiments run
  against the exact same htmx/Tailwind CDN pins as the real app rather
  than a separately hardcoded version. Own `Jinja2Templates` instance
  (not reused from `ui.py`) under `templates/sandbox/`, tagged
  `"Sandbox"` in the OpenAPI docs. Currently has one experiment,
  `hx-indicator` (`/sandbox/hx-indicator/`) — the four cases that
  proved out the loading-indicator pattern documented above and in the
  `htmx4` skill. Add new experiments as additional routes in this file
  plus a `templates/sandbox/*.html` file, linked from
  `sandbox/index.html`.
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
