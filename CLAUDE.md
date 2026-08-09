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
  exceptions to HTTP status codes. Each route depends on
  `require_permission(...)` (see `api/auth.py` below), never a role
  check. `POST /reviews/{id}/decision` is the one route needing **two**
  different permissions depending on the request body — `APPROVED` needs
  `Approve_Request`, `REJECTED` needs `Reject_Request` — which a single
  dependency can't express, so that route checks the permission matching
  the submitted `decision` value inside the handler itself rather than
  via the dependency list.
- **`api/auth.py`** — Keycloak JWT validation + **permission-check**
  dependency (`require_permission(permission: str)`) for the JSON API —
  not a role check. Temporal has zero concept of roles or permissions;
  this is the sole enforcement point for this front door.
  **The app checks permissions, never role names.** Five fine-grained
  permissions exist as plain Keycloak realm roles: `Create_Request`,
  `Update_Request`, `Cancel_Request` (Operator's), `Approve_Request`,
  `Reject_Request` (Manager's). `Operator` and `Manager` are themselves
  realm roles too, but **composite** ones — each just bundles the
  relevant permission roles as members; Keycloak auto-expands composite
  membership into the token's `realm_access.roles` claim, so a user
  holding `Operator` and a user granted its three permissions directly
  are indistinguishable to this app. `require_permission()` only ever
  checks for a specific permission string in that claim — it never
  checks for `Operator`/`Manager` by name, and doesn't know they exist.
  **This is what makes adding a new role a Keycloak-only config
  change**: a future `Auditor` role that can create and cancel requests
  but not approve/reject is just a new composite realm role bundling
  `Create_Request` + `Cancel_Request` in Keycloak's admin console — no
  code here changes, since none of it ever referenced `Operator`/
  `Manager` as concepts. `KEYCLOAK_ISSUER` is read lazily, on first
  actual call to a protected route, not at import time, so the app
  (including `/ui/*`, which has its own separate, unrelated
  `require_session_role()` check against the mock-auth session cookie —
  this permission model is API-only) runs fully without Keycloak
  configured at all.
- **`bff/mock_auth.py`** — session-cookie auth for the **POC UI only**
  (`bff/ui.py`). No password, no identity check — trusts whatever role
  the login form submitted. Never use for the JSON API. Swap-out path to
  real auth: replace what `login()` trusts with a Keycloak Authorization
  Code flow; keep storing `{"username", "role"}` in the session so
  `ui.py` doesn't need to change.
- **`bff/ui.py`** — server-rendered HTMX screens (`/ui/login`,
  `/ui/operator`, `/ui/manager`). Every route calls `workflow/service.py`,
  never Temporal/Postgres directly. Dialogs are HTML fragments swapped
  into `#dialog-container` (`_form_dialog.html` for Create/Edit,
  `_cancel_dialog.html` for Cancel, `_detail_dialog.html` for
  View/Review/Approve/Reject).
  **Every mutating action (Save, Cancel, Approve, Reject) follows the
  same confirm-dialog → close-immediately → spin-on-row-button
  sequence.** Opening the dialog (Edit/Cancel/Review buttons, plain
  `hx-get` into `#dialog-container`) is meant to feel instant — no
  artificial delay on that GET. The dialog's confirm button (Save /
  Confirm Cancel / Approve / Reject) is `type="button"`, not a form
  submit: its `onclick` reads whatever it needs from the dialog's own
  inputs into JS variables *first*, then clears `#dialog-container`'s
  `innerHTML` (closing the dialog client-side, before the request is even
  sent), then fires the actual mutation via `htmx.ajax()` — with `source`
  pointing at the **row's own stable-id action button**
  (`edit-btn-{request_id}`, `cancel-btn-{request_id}`,
  `review-btn-{request_id}`, each carrying its own `hx-indicator` span),
  not `this`, since the dialog element that would otherwise be `this` no
  longer exists by the time the response comes back. `target` is
  `#row-{request_id}`; `select: 'tr'` is required (see the
  `_operator_row_response.html`/`_manager_row_response.html` bullet below
  for why); `swap: 'outerHTML swap:400ms'` — the 400ms is a
  **perceptibility floor, not a correctness mechanism**: `_wait_until()`
  (below) already guarantees the response is the confirmed final state,
  but a real local round trip can be as fast as 60-90ms, too fast for a
  human to perceive the indicator's fade or its spin completing a visible
  rotation at all (see the `htmx4` skill's "Making a real round trip
  actually perceptible" section for the two independent CSS fixes this
  needed beyond just the delay). Create is the one exception still using
  a plain declarative form submit (`hx-post` on the `<form>` itself,
  swapping the whole `#request-list`) because there's no existing row to
  attach a `source` element to yet before the create succeeds; its
  spinner is the "+ New Request" button itself, and that button needs an
  *explicit* `hx-indicator="#create-spinner"` — omitting it means
  `.htmx-request` lands on the button rather than the spinner span (htmx
  falls back to the triggering element itself as the indicator when none
  is given), which still shows the spinner via htmx's own descendant CSS
  selector but silently skips the instant-show fix described below, since
  that fix's selector requires `.htmx-request` on the *same* element as
  the spinner.

  Every mutating route's *error* path needs to land back in the dialog,
  which no longer matches the row-scoped `target`/`source` those requests
  were fired with — those responses carry `_RETARGET_DIALOG_HEADERS`
  (`HX-Retarget: #dialog-container`, `HX-Reswap: innerHTML`, **and
  `HX-Reselect: #dialog-root`**). The `HX-Reselect` is not optional: the
  triggering action's `htmx.ajax()` call already set `select: 'tr'`, and
  per the real v4 bundle that selection filter is still in effect for
  *any* response to that request — including an error response, which
  has no `<tr>` in it at all. Without `HX-Reselect` resetting the filter,
  an error would silently render a blank dialog instead of the validation
  message, rather than erroring loudly. `#dialog-root` is the id every
  dialog fragment's own outer wrapper `<div>` carries, present in
  `_form_dialog.html`, `_cancel_dialog.html`, and `_detail_dialog.html`
  alike, specifically so this reselect target is stable across all three.
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
  keeps the same signature this codebase relies on — used directly by
  every dialog's confirm button (`_form_dialog.html`'s Save,
  `_cancel_dialog.html`'s Confirm Cancel, `_detail_dialog.html`'s
  Approve/Reject) for the close-dialog-then-fire-in-background pattern
  described in the `bff/ui.py` bullet above, since a declarative
  `hx-post` can't express "close this dialog and use a *different*
  element as the request's source." All requests now go
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
  Every spinner carries Tailwind's `transition-opacity duration-200`
  **unconditionally**, not gated behind `.htmx-request`, so the *hide*
  direction fades instead of cutting off instantly (htmx's own injected
  CSS only puts a `transition` on the active/show rule — see the `htmx4`
  skill's base "Loading indicators and swap timing" section). Layered on
  top of that, every spinner also carries
  `[&.htmx-request]:!duration-0` — a same-element conditional override
  that forces the *show* direction to be instant instead of taking the
  full 200ms — and `[animation-duration:.3s]` alongside `animate-spin` to
  speed up the rotation itself (Tailwind's default is 1000ms/turn, too
  slow to read as motion in a sub-100ms window). See the `htmx4` skill's
  "Making a real round trip actually perceptible" section for why both
  of these are needed together, and its "Loading indicators: which
  pattern to use, by whether a stable element exists" section for the
  close-dialog-then-target-a-stable-row-button architecture this whole
  bullet describes. `/sandbox/hx-indicator/` predates and proved out only
  the *base* indicator mechanism (the four simpler cases there, plus the
  MutationObserver timing harness at `/sandbox/hx-indicator/timing`) —
  it hasn't been updated to demonstrate the instant-show/fast-spin/
  close-dialog-first refinements layered on top since, so treat it as a
  historical record of the mechanism, not a live mirror of the current
  production pattern.
- **`bff/templates/_operator_row.html`, `_manager_row.html`** — each
  defines one Jinja `{% macro %}` rendering a single `<tr id="row-
  {request_id}">`, including that row's action button(s) with their
  stable ids (`edit-btn-{id}`/`cancel-btn-{id}` for operator,
  `review-btn-{id}` for manager) and embedded spinner spans.
  `_operator_list.html`/`_manager_list.html` `{% import %}` and call the
  macro once per row in their loop; `_operator_row_response.html`/
  `_manager_row_response.html` import and call the *same* macro for
  Save/Cancel/Approve/Reject's single-row responses. One definition, two
  call sites — the full-list and single-row renderings can never drift
  apart on markup. Extending a row (new column, new action button) means
  editing the macro once, not both list templates.
  **The single-row response templates wrap the macro's `<tr>` output in
  a real `<table><tbody>...</tbody></table>`** — a bare `<tr>` outside
  table context gets its own tags silently stripped by the browser's
  HTML parser on swap, leaving its cell contents as loose children of
  whatever `<tbody>` it lands in (see the `htmx4` skill's "Swapping
  `<tr>`/`<td>` fragments outside a `<table>` context" section for the
  full mechanism — this was a real, reproduced-and-fixed bug, not a
  theoretical concern). The `select: 'tr'` on every caller's
  `htmx.ajax()` (see the `bff/ui.py` bullet above) is what pulls the
  correctly-parsed `<tr>` back out of that wrapper for the actual swap.
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
  `hx-indicator` (`/sandbox/hx-indicator/`) — several cases that proved
  out the *base* `hx-indicator` mechanism (see the caveat on this in the
  `bff/templates/` bullet above — it predates and doesn't demonstrate the
  refinements layered on since) — and `hx-indicator/timing` (linked from
  that page) — a
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
- **The permission-based authorization design documented in the
  `api/auth.py`/`api/routes.py` bullets above is not implemented yet.**
  `require_role("operator")`/`require_role("manager")` are still the
  actual checks in code today; `require_permission()` doesn't exist.
  Keycloak itself also has zero provisioning anywhere in this repo (no
  realm export, no `docker-compose.yml` service, no composite-role
  setup) — the README's Keycloak setup section still describes the
  simpler two-realm-role version. This is the next implementation step,
  not yet done.
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
