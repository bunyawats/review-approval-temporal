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
    └── bff/                  # HTMX UI (real Keycloak session auth) + sandbox
        ├── __init__.py
        ├── ui.py / keycloak_session.py / sandbox.py
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
  Uses Keycloak's Authorization Services (Resources/Policies/Permissions
  — see the `keycloak-admin` skill for the general mechanism, JSON
  shape, and UMA ticket exchange details), not composite roles:
  `Operator`/`Manager` are plain realm roles; the five permissions
  (`Create_Request`, `Update_Request`, `Cancel_Request`,
  `Approve_Request`, `Reject_Request`) are Resources on the
  `review-approval` client, gated by role-based Policies (`Operator
  Policy`/`Manager Policy`) via matching Permissions — see the
  `keycloak` bullet below for this project's exact structure.
  **Consequence worth remembering when implementing
  `require_permission()`**: since the five permissions are Resources,
  not roles, they never appear in the plain access token's
  `realm_access.roles` claim — checking one needs a real UMA ticket
  exchange (a second Keycloak call, or a cached RPT), not just decoding
  a different claim from the same token. Adding a new role (e.g.
  `Auditor`) stays a pure Keycloak config change either way: new role +
  new Policy + add that Policy to the relevant Permissions'
  `applyPolicies` — no code here ever references role names, only
  Resource names. `KEYCLOAK_ISSUER` is read lazily, on first actual call
  to a protected route, not at import time, so the app (including
  `/ui/*`, which has its own separate `require_session_role()`/
  `require_permission()` checks (see `bff/keycloak_session.py` below))
  runs fully without Keycloak configured at all.
- **`bff/keycloak_session.py`** — real Keycloak session auth for `/ui/*`
  (Authorization Code flow), replacing the earlier mock `bff/mock_auth.py`
  (deleted). `login()`'s old "trust the submitted role" is gone;
  `build_authorize_url()`/`complete_login()` do a real redirect + code
  exchange against Keycloak. **Two authorization mechanisms, both
  permanent, not one superseding the other**: `require_session_role()`
  gates *screen* access (which of `/ui/operator`/`/ui/manager` a session
  can see — `Operator`/`Manager` stay plain realm roles since there's no
  Resource/Permission for "which screen," so a role check is the right
  tool), while `require_permission()`/`check_permission()` (added Phase
  3) gate the five *mutating actions* via the same UMA ticket exchange
  (`workflow/keycloak_auth.get_permissions()`) `api/auth.py` uses for the
  REST API — see `bff/ui.py` below for how each route uses which. The
  session's `role` field ("operator"/"manager", lowercase, derived once
  at login from `realm_access.roles`) is what `require_session_role()`
  checks — a permanent field, not a bridge slated for removal.
  **Deliberately does NOT store `refresh_token`/`id_token`** — Starlette's
  `SessionMiddleware` signs the whole session into one browser cookie
  (no server-side store), and all three JWTs together measured ~4.5KB
  signed, over the ~4KB limit real browsers enforce per cookie (measured
  directly, not assumed — `curl` doesn't enforce that limit, so a
  curl-only test of this would have silently passed while a real browser
  failed). No token refresh this phase either, for the same
  keep-it-small reason plus not being needed yet — an expired access
  token (5 min lifetime) just forces re-login rather than transparently
  refreshing. Logout redirects through Keycloak's own end-session
  endpoint using `client_id` (not `id_token_hint`, since none is
  stored) — this makes Keycloak show a confirmation page instead of
  logging out silently (real, documented OIDC RP-initiated-logout
  behavior, not a bug), a deliberate trade against the cookie-size
  constraint. See the `keycloak-admin` skill's "Authorization Code flow"
  section for the exact gotchas hit building this (the `post.logout.
  redirect.uris` client attribute, its `##`-delimited-string format, and
  why the commonly-documented `"+"` shorthand didn't work against this
  Keycloak version).
- **`bff/ui.py`** — server-rendered HTMX screens (`/ui/login`,
  `/ui/operator`, `/ui/manager`). Every route calls `workflow/service.py`,
  never Temporal/Postgres directly.
  **Permission enforcement (Phase 3)**: `new_form`/`create_request`,
  `edit_form`/`update_request`, and `cancel_form`/`cancel_request_route`
  are gated by `Depends(require_permission("Create_Request"))` etc.;
  `manager_decision` branches `Approve_Request`/`Reject_Request` via an
  inline `check_permission()` call based on the submitted `decision`
  (can't be a single `Depends()` since the required permission depends
  on request body, same reasoning as `api/routes.py`'s `submit_decision`)
  — note `manager_decision` is *also* still gated by
  `require_session_role("manager")` first, so an operator session gets
  `403 requires role: manager` before the permission check ever runs
  (unlike the REST API, which has no role gate on that route, only the
  permission check — a real behavioral difference between the two front
  doors, not a bug). `_user_permissions(user)` (a thin wrapper that
  fails closed to an empty set on any Keycloak error) is called once per
  page/row render and passed into templates as `permissions`, so button
  visibility reflects what's actually granted — see the `bff/templates/`
  bullet below. Dialogs are HTML fragments swapped
  into `#dialog-container` (`_form_dialog.html` for Create/Edit,
  `_cancel_dialog.html` for Cancel, `_detail_dialog.html` for
  View/Review/Approve/Reject).
  **Every mutating action (Save, Cancel, Approve, Reject) follows the
  same pattern**: dialog opens instantly (plain `hx-get`, no artificial
  delay), its confirm button (`type="button"`, not a form submit) reads
  whatever it needs from the dialog's own inputs into JS variables
  *first*, clears `#dialog-container`'s `innerHTML` (closing it
  client-side before the request is even sent), then fires the mutation
  via `htmx.ajax()` — `source` points at the **row's own stable-id
  action button** (`edit-btn-{request_id}`, `cancel-btn-{request_id}`,
  `review-btn-{request_id}`, each with its own `hx-indicator` span), not
  `this`, since the dialog element is already gone by the time the
  response arrives. `target` is `#row-{request_id}`, `select: 'tr'` is
  required (see the `_operator_row.html` bullet below), `swap:
  'outerHTML swap:400ms'` — the 400ms is a **perceptibility floor, not a
  correctness mechanism** (`_wait_until()` below already guarantees the
  response is confirmed-final; a real round trip is just too fast,
  60-90ms, to see happen otherwise — see the `htmx4` skill's "Making a
  real round trip actually perceptible" section). Create is the one
  exception, still a plain declarative form submit swapping the whole
  `#request-list` (no row exists yet to attach `source` to); its button
  needs an *explicit* `hx-indicator="#create-spinner"` — see the `htmx4`
  skill for why omitting it would matter.

  Every mutating route's *error* path retargets to the dialog via
  `_RETARGET_DIALOG_HEADERS` (`HX-Retarget`/`HX-Reswap`/`HX-Reselect` —
  the last one needed because the triggering request's `select: 'tr'`
  would otherwise still apply and blank the error dialog; see the
  `htmx4` skill's `<tr>`/`HX-Reselect` section for why). `#dialog-root`
  is the id every dialog fragment's own outer wrapper carries
  (`_form_dialog.html`, `_cancel_dialog.html`, `_detail_dialog.html`),
  so this reselect target is stable across all three.
- **`bff/templates/`** — Jinja2 templates. `base.html` is the shell;
  `_*.html` are HTMX fragments, not full pages. This project's Starlette
  version requires **`TemplateResponse(request, name, context)`**
  (request first) — the older `TemplateResponse(name, {"request": ...})`
  form breaks here. Styling is Tailwind via a CDN script in
  `base.html` — no custom `<style>` blocks, no custom CSS classes; use
  Tailwind utility classes directly. Status-badge colors are a Jinja
  dict literal (`badge_classes`) **duplicated in three templates**
  (`_operator_row.html`, `_manager_row.html`, `_detail_dialog.html`) —
  a new status value needs all three updated. Templates ship as package
  data (`pyproject.toml`'s `[tool.setuptools.package-data]`, which uses a
  recursive `templates/**/*.html` glob so subdirectories like
  `templates/sandbox/` are included too); new `.html` files need no
  config change, other asset types would.
  **Deliberately tracks the latest major of both htmx and Tailwind** —
  pinned to **htmx 4.x beta** (`https://unpkg.com/htmx.org@4.0.0-beta6`)
  and **Tailwind v4** via `https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4`
  — *not* `cdn.tailwindcss.com` (the old Play CDN, permanently stuck on
  v3). See the `htmx4` skill for what actually changed in v4 vs 2.x, how
  to re-verify a version bump against the real bundle rather than
  changelog prose, and the general loading-indicator/swap-timing
  mechanics referenced throughout this file. Two things this project
  specifically depends on from that skill's "verified changes" list:
  `_form_dialog.html`'s inline `<script>` (sample-payload auto-fill)
  relies on v4's now-unconditional script execution on swap, and every
  dialog's confirm button uses `htmx.ajax()` rather than declarative
  `hx-post`, specifically to get the close-dialog-then-fire-elsewhere
  pattern described in the `bff/ui.py` bullet above (a declarative
  `hx-post` can't express "use a *different* element as the source").
  Tailwind v4's `shadow`/`rounded`/`blur` scale renames and `border`
  default-color change don't affect this codebase today (only suffixed
  variants used, `border`/`border-b` always paired with an explicit
  color) — grep for bare `border`/`rounded`/`shadow`/`blur` before
  assuming a new utility addition is still safe.
  Every spinner carries `transition-opacity duration-200` unconditionally
  (smooth *hide*), `[&.htmx-request]:!duration-0` (instant *show*), and
  `[animation-duration:.3s]` alongside `animate-spin` (Tailwind's default
  1000ms/turn doesn't read as motion in a sub-100ms window) — see the
  `htmx4` skill's "Making a real round trip actually perceptible"
  section for why all three are needed together.
  `/sandbox/hx-indicator/` predates these refinements — treat it as a
  historical record of the base mechanism, not a live mirror of the
  current pattern.
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
  a real `<table><tbody>...</tbody></table>`** — required because a bare
  `<tr>` swapped outside table context gets its tags silently stripped
  by the browser (a real, reproduced-and-fixed bug here — see the
  `htmx4` skill's "Swapping `<tr>`/`<td>` fragments" section for the
  full mechanism). `select: 'tr'` on every caller's `htmx.ajax()` (see
  `bff/ui.py` above) is what pulls the correctly-parsed `<tr>` back out
  of that wrapper for the actual swap.
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
- **`keycloak`** — `quay.io/keycloak/keycloak:26.0`, `start-dev
  --import-realm`, admin console (`admin`/`admin`) at port `8080`,
  published to the host. Realm defined declaratively in
  **`keycloak/import/myrealm-realm.json`** (mounted read-only into
  `/opt/keycloak/data/import`) — the single source of truth; edit it and
  recreate the container (`docker compose down -v keycloak && docker
  compose up -d keycloak`, **not** `restart` — see the `keycloak-admin`
  skill for why a plain restart silently skips re-importing). That skill
  covers the general Docker/realm-import/Authorization-Services
  mechanics; this bullet is just what's specific to this project:
  - 2 plain (non-composite) realm roles: `Operator`, `Manager`
  - 5 Resources on the `review-approval` client: `Create_Request`,
    `Update_Request`, `Cancel_Request`, `Approve_Request`,
    `Reject_Request`
  - 2 role-based Policies (`Operator Policy`/`Manager Policy`) and 5
    Permissions binding each Resource to the right one — Create/Update/
    Cancel via Operator, Approve/Reject via Manager
  - `review-approval` is a confidential client, `secret:
    dev-secret-change-me` (fine for `start-dev`-only local dev, plainly
    checked into the JSON — never do this anywhere real)
  - 4 demo users, password `password`: `operator1`/`operator2` →
    `Operator`, `manager1`/`manager2` → `Manager`
  Verified end to end, not just configured: a real UMA ticket exchange
  for `operator1` returns exactly `Create_Request`/`Update_Request`/
  `Cancel_Request`, `manager1` exactly `Approve_Request`/`Reject_Request`
  — and a real token clears JWT signature/issuer validation against this
  instance fine (confirmed via `bff`'s actual `403 requires role:
  operator` response — wrong, stale role-name check, not an auth
  failure; see "Known gaps"). `keycloak/list-permissions-by-role.sh`
  audits the live Policy→Permission→Resource config from the command
  line (a filled-in version of the `keycloak-admin` skill's general
  script).
  **The 4 core app services are commonly run natively instead**, with
  only `keycloak` in Docker — see "Running locally" below.

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

## Full Keycloak integration: complete

**`keycloak/INTEGRATION_PLAN.md` has the full phased history and status
tracker** — real Keycloak login (Authorization Code flow) for `/ui/*`,
fine-grained permission enforcement (via UMA ticket exchange) on every
mutating route in both `api/` and `bff/`, and a 35-test suite (11 unit,
24 integration) covering it end to end. Read that file before touching
auth-related code — it also documents real gotchas hit along the way
(cookie-size limits, `post.logout.redirect.uris`, etc.), several of
which are also captured in the `keycloak-admin` skill.

## Known gaps

- No timeout on "wait for Manager decision" — requests can wait forever.
- No notification activity (email/Slack) on request creation or decision.
- `verify_aud=False` in `api/auth.py` — needs a real audience once the
  Keycloak client is finalized.
- No access-token refresh in `bff/keycloak_session.py` — a 5-minute-old
  session just forces re-login. Deliberate simplification, not an
  oversight — see that module's docstring.
- No caching on either front door's permission checks — every mutating
  action does a live UMA ticket exchange against Keycloak, no RPT/result
  caching. Deliberate ("no caching in the first pass" per
  `keycloak/INTEGRATION_PLAN.md`), acceptable latency cost for now; the
  natural fix if it ever matters is caching the RPT for its own validity
  window, not fixed here.
- No automated check that every `KNOWN_REVIEW_TYPES` entry has a worker
  polling its queue.
- Test suite covers the full Keycloak integration (auth core, REST API
  enforcement, BFF login, BFF permission enforcement) — no
  workflow/activity tests yet (`tests/` exists now, under the repo root
  per the "Testing changes" section below; extend it, don't start a
  second test tree).

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

**Hybrid (the common case in practice): everything above runs natively,
Keycloak alone runs in Docker** — `docker compose up -d keycloak` (only
that service starts). `.env`'s
`KEYCLOAK_ISSUER=http://localhost:8080/realms/myrealm` (the
`.env.example` default) is already correct for this; it's only a
*Dockerized* `bff` that needs the Docker-internal `http://keycloak:8080/...`
form instead (`docker-compose.yml`'s own `bff` service already has it
right). See the `keycloak-admin` skill's "Docker networking gotchas"
section for why a natively-run Postgres and a Dockerized one can coexist
on the same host port without conflict — confirmed true here, but don't
lean on it; if in doubt, stop whichever instance you're not using
(`docker compose stop <service>`, or check with `lsof -nP -iTCP:<port>
-sTCP:LISTEN`).

## Testing changes

`tests/` at the repo root (not inside the package), split
`tests/unit/` (no live services — mock at the HTTP layer with `respx`
for anything hitting Keycloak; `PyJWKClient` fetches via `urllib`, not
`httpx`, so JWT-validation tests instead patch the key-resolution step
directly and let the real `jwt.decode()` run against a locally-generated
keypair — see `tests/unit/test_keycloak_auth.py`) and
`tests/integration/` (needs the real local stack — Keycloak, Postgres,
Temporal, worker — up; marked `@pytest.mark.integration`, deselect with
`pytest -m "not integration"`). No workflow/activity tests yet; when
adding them, prefer Temporal's `temporalio.testing.WorkflowEnvironment`
(time-skipping test environment) over hitting a real Temporal server.
