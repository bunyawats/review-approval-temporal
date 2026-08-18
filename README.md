# Review / Approval Workflow

Temporal (Python SDK) + FastAPI BFF + PostgreSQL + Keycloak.

## Project structure

```
review-approval/
├── pyproject.toml           # single source of truth for deps + packaging
├── Dockerfile
├── docker-compose.yml
├── db/
│   ├── schema.sql           # applied by db/init/ on first Postgres boot
│   └── init/
└── review_approval/         # the installable package -- everything else
    ├── app.py               # FastAPI app: assembles bff + api + sandbox routers
    ├── workflow/            # Temporal-side: workflow, activities, worker, task queues
    │   ├── workflows.py
    │   ├── activities.py
    │   ├── worker.py
    │   ├── task_queues.py
    │   ├── service.py       # shared business logic -- both bff/ and api/ call this
    │   └── schemas.py       # per-review-type payload validation registry
    ├── api/                 # JSON REST surface (real Keycloak auth)
    │   ├── routes.py
    │   └── auth.py
    └── bff/                 # HTMX UI (real Keycloak session auth) + sandbox
        ├── ui.py
        ├── keycloak_session.py
        ├── sandbox.py       # /sandbox/* -- standalone htmx experiments
        └── templates/       # Jinja2/HTMX templates, Tailwind via CDN
```

Everything lives under the one `review_approval` package — install it
once (`pip install -e .` natively, or via the Dockerfile) and every
module imports every other module by its real package path.

## Architecture

See [`docs/architecture.html`](docs/architecture.html) for a diagram of
the request flow — open it in a browser (it's self-contained, no server
needed). It's the one place that draws the async write path end to end:
`service.py` signals Temporal and gets an immediate ack, but the actual
Postgres row only exists once `worker-activity` later runs the matching
`persist_*` activity, which is why `service.py` polls Postgres in a
bounded loop before answering the caller.

- **`workflow/workflows.py`** — `ReviewApprovalWorkflow`. Payload-agnostic:
  it carries `review_type` + `payload` through untouched. Waits durably on
  a signal for the Manager's decision, or the requester's cancellation.
  Also recovers gracefully from a native Temporal *cancel* (e.g. the
  Web UI's Cancel button, not our own signal) via a `try`/`except
  asyncio.CancelledError` around that wait, so the Postgres row still ends
  up `CANCELLED` instead of orphaned at `PENDING_REVIEW` — see `CLAUDE.md`
  for the full mechanism and why a Temporal *terminate* can't be handled
  the same way.
- **`workflow/activities.py`** — the only code that touches Postgres.
- **`workflow/worker.py`** — long-lived process that executes
  workflow/activity code. `WORKER_MODE` env var (`both` / `workflow` /
  `activity`) controls what it registers; `REVIEW_TYPE` controls which
  review type's task queue(s) it polls (unset = all of them). Docker
  Compose runs it split by role as `worker-workflow` (no DB credentials)
  and `worker-activity` (needs `DATABASE_URL`).
- **`workflow/task_queues.py`** — `KNOWN_REVIEW_TYPES` and the
  per-review-type task queue naming scheme, so worker capacity can scale
  independently per type instead of one shared pool serving everything.
- **`workflow/service.py`** — shared business logic (ownership checks,
  status checks, starts/signals workflows). Neither front door talks to
  Temporal or Postgres directly — only this module does, which is what
  keeps `bff/` and `api/` from drifting out of sync on what's actually
  allowed. Also recovers from a *deleted* workflow execution (a Temporal
  admin ran `temporal workflow delete` on it, not just cancel/terminate) —
  self-heals the row straight to `CANCELLED` the next time someone tries
  to act on it, since there's no workflow execution left to signal at
  all. See `CLAUDE.md` for the full mechanism.
- **`api/`** — the JSON REST surface. Validates Keycloak JWTs, enforces
  fine-grained permissions (`Create_Request`, `Update_Request`,
  `Cancel_Request`, `Approve_Request`, `Reject_Request`) rather than
  role names, calls into `workflow/service.py`. Temporal itself has no
  concept of roles or permissions — this is the sole enforcement point
  for that front door. See the Keycloak setup section below for how
  `Operator`/`Manager` map onto these permissions.
- **`bff/`** — the server-rendered HTMX UI, real Keycloak login
  (Authorization Code flow — see `keycloak/INTEGRATION_PLAN.md`) plus
  the same fine-grained permission checks as `api/` on every mutating
  route, alongside a role check that gates which screen
  (`/ui/operator`/`/ui/manager`) a session can see, plus `/sandbox/*`, a
  standalone playground for htmx experiments (no auth, no
  Temporal/Postgres).
- **`app.py`** — assembles the actual FastAPI app: lifespan (Temporal
  client + Postgres pool), session middleware, and mounts all three
  routers (`bff.ui`, `bff.sandbox`, `api.routes`).
- **`db/schema.sql`** — Postgres table for listing/reporting/audit.
  Temporal is the source of truth for *live* state; Postgres is the
  queryable record. `workflow_id` is nullable — cleared back to `NULL`
  when a row's underlying Temporal execution has been deleted and no
  longer exists to point at.

Adding a new review type touches two files: a Pydantic model in
`review_approval/workflow/schemas.py`, and its type string added to
`KNOWN_REVIEW_TYPES` in `review_approval/workflow/task_queues.py`. An
assertion at import time catches it if these two drift apart.

## Setup

### Option A: Docker Compose (recommended for local dev)

```bash
docker compose up --build
```

This starts everything: Postgres (with two databases — one for Temporal's
own persistence, one for the app, auto-created by `db/init/*.sh`), the
Temporal Service + Web UI, two worker services (`worker-workflow`,
`worker-activity`), Keycloak (realm auto-imported, see the Keycloak
section below), and the `bff`. No manual `createdb`, no manual
`pip install`, no manual Keycloak setup.

Scale worker capacity for a rough local HA test:

```bash
docker compose up --build --scale worker-activity=3
```

- App / UI: **http://localhost:8000** (redirects to `/ui/login`, real
  Keycloak login)
- JSON API docs (Swagger UI): **http://localhost:8000/docs**
- Temporal Web UI: **http://localhost:8233** (also real Keycloak login —
  see below, one extra one-time setup step required)
- Keycloak admin console: **http://localhost:8080** (`admin`/`admin`)
- Postgres: `localhost:5433` (`temporal`/`temporal`, databases `temporal`
  and `review_approval`) — off the standard `5432` so it doesn't collide
  with a natively-installed Postgres also listening on the host

Only `keycloak` alone is commonly run this way while everything else
runs natively — see "Running locally" in `CLAUDE.md` for that hybrid
setup (`docker compose up -d keycloak`, nothing else). Note that
Temporal Web UI's Keycloak login (below) only works via the Dockerized
`temporal`/`temporal-ui` services — the native `temporal server
start-dev` CLI's bundled UI has no such option at all, so using this
means running `temporal`/`temporal-ui` via Compose even in an otherwise
hybrid/native setup.

**Required one-time setup for Temporal Web UI login**: add this to
`/etc/hosts` (needs `sudo`):

```bash
echo '127.0.0.1 keycloak' | sudo tee -a /etc/hosts
```

Without it, clicking "Log in" on Temporal Web UI redirects to a
`keycloak` hostname your browser can't resolve — Temporal UI builds
that redirect directly from server-side config, with no separate
browser-facing URL option. Once logged in, use **`temporal-admin1`**
(password `password`) — the only demo user with the `TemporalAdmin`
role, which Temporal Web UI login is restricted to (any of the other 4
demo users get a real `401 Access denied` from Keycloak itself). This
gates *login only* — every logged-in user still sees every workflow's
full payload, unfiltered; see `CLAUDE.md`'s "Known gaps" for why
per-user authorization inside Temporal itself isn't implemented.

To rebuild after code changes: `docker compose up --build`. To reset the
database: `docker compose down -v`.

### Option B: Run everything natively

<details>
<summary>Manual setup steps</summary>

#### 1. Postgres

```bash
createdb review_approval
psql review_approval -f db/schema.sql
```

#### 2. Temporal

```bash
temporal server start-dev
```

Starts the Temporal Service on `localhost:7233` and the Web UI at
`http://localhost:8233`.

#### 3. Keycloak (required for both `/ui/*` and the JSON API)

> **Note:** both `api/` (REST) and `/ui/*` (BFF) fully enforce the
> permissions described below on every mutating route — see
> `keycloak/INTEGRATION_PLAN.md`'s status tracker. `/ui/*` additionally
> gates which *screen* (`/ui/operator` vs `/ui/manager`) a session can
> see via a simpler role check, since there's no Resource/Permission for
> "which screen" — a deliberate, permanent split, not a gap. The
> permissions below are Resources, not roles, so checking one needs a
> UMA ticket exchange, not just a claim lookup on the token.

Run Keycloak in Docker (works fine standalone, without the rest of
`docker-compose.yml`'s services):

```bash
docker compose up -d keycloak
```

This imports a ready-made realm from `keycloak/import/myrealm-realm.json`
automatically on every start — no manual admin-console setup needed.
Uses Keycloak's **Authorization Services** (Resources + Policies +
Permissions) rather than plain/composite roles, so that "which role
grants which action" is its own editable object instead of being baked
into a role's membership list:

- Three plain (non-composite) realm roles: `Operator`, `Manager`,
  `TemporalAdmin` (the last gates Temporal Web UI login only, via a
  custom Keycloak authentication flow, not this Resources/Policies/
  Permissions mechanism — see `CLAUDE.md`'s `temporal-ui` bullet)
- Five **Resources** on the `review-approval` client, one per action:
  `Create_Request`, `Update_Request`, `Cancel_Request`,
  `Approve_Request`, `Reject_Request`
- Two role-based **Policies** (`Operator Policy`, `Manager Policy`) and
  five **Permissions** binding each Resource to the right one —
  Create/Update/Cancel via `Operator Policy`, Approve/Reject via
  `Manager Policy`
- `review-approval` is a **confidential** client (`secret:
  dev-secret-change-me`) — required, since a public client can't have
  Resources/Policies/Permissions at all
- A second confidential client, `temporal-ui` (`secret:
  temporal-ui-dev-secret-change-me`), used only for Temporal Web UI's
  own OIDC login — unrelated to this Resources/Policies/Permissions
  setup
- Five demo users, password `password` for all: `operator1`/`operator2`
  (`Operator`), `manager1`/`manager2` (`Manager`), `temporal-admin1`
  (`TemporalAdmin`)

Adding a new role later (e.g. an `Auditor` who can create and cancel but
not approve/reject) is pure Keycloak config: new `Auditor` role, new
`Auditor Policy` requiring it, add that policy to the
`Create_Request`/`Cancel_Request` Permissions' `applyPolicies`. No
application code changes. To change the realm's own definition, edit
`keycloak/import/myrealm-realm.json` and recreate the container
(`docker compose down -v keycloak && docker compose up -d keycloak`) — a
plain `restart` reuses the running container's state and silently skips
re-importing your edit.

Admin console: `http://localhost:8080` (`admin`/`admin`). Get a token to
test with (note the `client_secret` — see above):

```bash
curl -s -X POST http://localhost:8080/realms/myrealm/protocol/openid-connect/token \
  -d "client_id=review-approval" -d "client_secret=dev-secret-change-me" \
  -d "grant_type=password" -d "username=operator1" -d "password=password"
```

Checking which permissions a token actually carries requires a second
call — a **UMA ticket exchange**, trading the access token above for the
resource-server's own view of what it's allowed to do. This is exactly
what `api/auth.py`'s and `bff/keycloak_session.py`'s `require_permission()`/
`check_permission()` do on every mutating route (via
`workflow/keycloak_auth.get_permissions()`), one live call per check, no
caching — see `CLAUDE.md`'s "Known gaps":

```bash
curl -s -X POST http://localhost:8080/realms/myrealm/protocol/openid-connect/token \
  -H "Authorization: Bearer $TOKEN" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:uma-ticket" \
  -d "audience=review-approval" \
  -d "client_id=review-approval" -d "client_secret=dev-secret-change-me" \
  -d "response_mode=permissions"
# operator1 -> [{"rsname": "Create_Request", ...}, {"rsname": "Update_Request", ...}, {"rsname": "Cancel_Request", ...}]
```

`KEYCLOAK_ISSUER` in `.env.example` (`http://localhost:8080/realms/myrealm`)
already matches this out of the box for a natively-run `bff`. If `bff`
itself runs in Docker instead, it needs the Docker-internal hostname —
`docker-compose.yml`'s own `bff` service already sets this correctly
(`http://keycloak:8080/realms/myrealm`).

#### 4. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # editable install -- local edits picked up immediately
cp .env.example .env        # edit with your real values
export $(cat .env | xargs)  # or use a tool like direnv/python-dotenv
```

#### 5. Run the worker (separate terminal)

```bash
python -m review_approval.workflow.worker
```

#### 6. Run the app (separate terminal)

```bash
uvicorn review_approval.app:app --reload --port 8000
```

</details>

## POC UI (HTMX, real Keycloak login)

Whichever setup option you used, the UI lives at `/ui/*`, in the same
FastAPI app as the JSON API. Requires Keycloak up (see the Keycloak
setup section above) — unlike the JSON API, `/ui/*` isn't usable
without it.

Open **http://localhost:8000** — it redirects to a login screen with a
"Log in with Keycloak" link. Use any of the four demo users
(`operator1`/`operator2`/`manager1`/`manager2`, password `password`).

- **Operator screen** (`/ui/operator`) — shows only *your* requests. "+ New
  Request" opens a dialog to pick a review type and paste a JSON payload.
  Pending requests can be edited or cancelled; decided/cancelled ones are
  view-only.
- **Manager screen** (`/ui/manager`) — shows *all* requests across every
  operator. Clicking a row opens a dialog with the full JSON payload;
  pending ones get Approve/Reject buttons, decided ones are view-only.

Both screens are paginated, 10 rows per page, with Prev/Next controls
and a "Showing X–Y of Z" count. Both also poll every 5 seconds so a
Manager's decision shows up on the Operator's screen (and vice versa)
without a manual refresh — the poll and Prev/Next both reuse a cached
row count (`query_id`, minted server-side, round-tripped by the page)
rather than re-running `COUNT(*)` on every tick; see
`docs/PAGINATION_PLAN.md` for the full design (including why the
Operator screen's cache reuse needs an extra check that the manager
screen's doesn't — the cached filter has to be re-verified against the
logged-in session before it's trusted, so one operator's session can
never end up paging through another's requests).

The UI calls `review_approval/workflow/service.py` directly (not the
JSON API) after establishing its own Keycloak-backed session — both
front doors funnel through the same `service.py` functions, so there's
one source of truth for what's allowed.

## htmx sandbox

`http://localhost:8000/sandbox/` — standalone htmx experiments, no auth,
no Temporal/Postgres, kept permanently alongside the app rather than
thrown away after use. Runs against the app's actual pinned htmx/Tailwind
CDN versions (via `base.html`). Currently has two experiments:
`hx-indicator` (`/sandbox/hx-indicator/`), which proved out the base
`hx-indicator` mechanism, and `hx-indicator/timing`, a
MutationObserver-instrumented harness for measuring exact swap timing
instead of eyeballing it.

## Try it (JSON API)

Requires `KEYCLOAK_ISSUER` set and a real Keycloak realm running (see
the Keycloak setup section above) — not needed for the `/ui/*` screens
above.

`review-approval` is a **confidential** client (has a secret), needed
for its Authorization Services / Resources+Policies+Permissions setup —
so token requests need `client_secret` too, unlike a plain public
client. `keycloak/get-token.sh` wraps this:

```bash
TOKEN=$(./keycloak/get-token.sh -u operator1)
MANAGER_TOKEN=$(./keycloak/get-token.sh -u manager1)
```

(demo users, password `password`: `operator1`/`operator2` → Operator,
`manager1`/`manager2` → Manager — see `-h` for all flags.)

**Running the full `docker compose up` stack?** Add `-D`: `bff`
validates a token's `iss` claim against its own `KEYCLOAK_ISSUER`, which
`docker-compose.yml` sets to the Docker-internal
`http://keycloak:8080/realms/myrealm` — a token fetched from the host
normally has `iss=http://localhost:8080/...` instead and gets rejected
with `Invalid issuer`. `-D` fetches the token from inside the `bff`
container instead, so `iss` matches:

```bash
TOKEN=$(./keycloak/get-token.sh -u operator1 -D)
```

Not needed for the "hybrid" setup (`bff` run natively, only `keycloak`
in Docker) — there, both the host and `bff` reach Keycloak via
`localhost:8080`, so plain (no `-D`) tokens already match.

### Via Swagger UI (interactive)

`http://localhost:8000/docs` lets you drive every JSON API route from
the browser, no `curl` needed:

1. Get a token as above (`./keycloak/get-token.sh -u operator1`, `-D`
   if running the full `docker compose up` stack) and copy it.
2. Open `http://localhost:8000/docs`.
3. Click **Authorize** (top right, padlock icon), paste the token into
   the value field — just the raw token, no `Bearer ` prefix, Swagger
   adds that itself — then **Authorize** → **Close**.
4. Expand any route, click **Try it out**, fill in params/body, then
   **Execute**. The auth header is attached automatically to every
   request from here on.

Notes specific to this app:
- Tokens expire in 5 minutes — a `401` mid-testing usually just means
  it's stale; get a fresh one and re-Authorize.
- `POST /reviews/{id}/decision` needs a **Manager** token for
  `APPROVED`/`REJECTED` (an Operator token gets `403` there); Create/
  Update/Cancel need an **Operator** token. Re-Authorize with a
  different user's token to switch roles.
- Watch the workflow execute live in the Temporal Web UI at
  `http://localhost:8233` while you drive it from Swagger.

### Via curl

Create a review request (as Operator):

```bash
curl -X POST http://localhost:8000/reviews \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "review_type": "purchase_order",
    "payload": {"vendor": "Acme Corp", "amount": 1500.00, "line_items": ["widgets", "gadgets"]}
  }'
```

Check status (as either role):

```bash
curl http://localhost:8000/reviews/<request_id> -H "Authorization: Bearer $TOKEN"
```

Approve/reject (as Manager, using a Manager token):

```bash
curl -X POST http://localhost:8000/reviews/<request_id>/decision \
  -H "Authorization: Bearer $MANAGER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision": "APPROVED", "comment": "Looks good"}'
```

Cancel (as the original Operator, comment optional):

```bash
curl -X POST http://localhost:8000/reviews/<request_id>/cancel \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"comment": "Submitted by mistake"}'
```

List with pagination (as either role -- `POST`, not `GET`, so `page`/
`page_size`/`query_id`/`filter` all live in the JSON body):

```bash
curl -X POST http://localhost:8000/reviews/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"page": 0, "page_size": 10, "filter": {"review_type": "purchase_order"}}'
```

The response's `query_id` can be replayed (with `filter` omitted) to page
through the same result set without re-running `COUNT(*)`, as long as
it's within the 30s cache TTL:

```bash
curl -X POST http://localhost:8000/reviews/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"page": 1, "query_id": "<query_id from previous response>"}'
```

You can also watch the workflow execute live in the Temporal Web UI at
`http://localhost:8233`.

## Scaling worker capacity per review type

Each review type gets its own Temporal task queue
(`task_queue_for_review_type()` in
`review_approval/workflow/task_queues.py`). `worker.py` reads a
`REVIEW_TYPE` env var:

- **Unset** (Compose's default) — one process polls every known type's
  queue. Simple, fine for local dev or a small deployment.
- **Set** (e.g. `REVIEW_TYPE=purchase_order`) — that process polls only
  that type's queue. Give a high-volume or slow-activity review type its
  own Deployment and replica count in Kubernetes, independent of every
  other type.

`docker-compose.yml` has a commented-out example service showing this;
Compose itself keeps both worker services `REVIEW_TYPE`-unset to stay
simple.

## Notes / things to harden before production

- `docker-compose.yml` is for **local dev only** — it's not the
  Kubernetes deployment shape. See `CLAUDE.md`'s Kubernetes section for
  the production topology.
- `/ui/*` has real Keycloak login and the same fine-grained permission
  checks as the REST API on every mutating route
  (`review_approval/bff/keycloak_session.py`) — see
  `keycloak/INTEGRATION_PLAN.md`. No access-token refresh yet (a
  5-minute-old session just forces re-login), and no caching on either
  front door's permission checks (every check is a live UMA call). Both
  are deliberate simplifications, not silent gaps — see `CLAUDE.md`'s
  "Known gaps".
- `verify_aud=False` in `review_approval/api/auth.py` — set and verify a
  real audience once the Keycloak client is configured.
- No timeout on "wait for Manager decision" — consider adding a
  `workflow.wait_condition(..., timeout=...)` plus a reminder/escalation
  activity.
- A native Temporal **cancel** is recovered gracefully; a **terminate** is
  not, and can't be from inside the workflow (no event is ever delivered
  to catch) — prefer cancel over terminate on this app's workflows. A
  deleted workflow execution is recovered too, but only lazily, the next
  time someone acts on the affected row — not proactively on list
  screens. See `CLAUDE.md`'s "Known gaps" for the full detail.
- No notification step (email/Slack) on request creation or decision.
- Adding a review type with no worker actually polling its queue leaves
  requests stuck at `PENDING_REVIEW` forever with no error anywhere.
