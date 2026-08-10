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
    └── bff/                 # HTMX UI (mock auth) + sandbox
        ├── ui.py
        ├── mock_auth.py
        ├── sandbox.py       # /sandbox/* -- standalone htmx experiments
        └── templates/       # Jinja2/HTMX templates, Tailwind via CDN
```

Everything lives under the one `review_approval` package — install it
once (`pip install -e .` natively, or via the Dockerfile) and every
module imports every other module by its real package path.

## Architecture

- **`workflow/workflows.py`** — `ReviewApprovalWorkflow`. Payload-agnostic:
  it carries `review_type` + `payload` through untouched. Waits durably on
  a signal for the Manager's decision, or the requester's cancellation.
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
  keeps `bff/` (mock auth) and `api/` (real Keycloak auth) from drifting
  out of sync on what's actually allowed.
- **`api/`** — the JSON REST surface. Validates Keycloak JWTs, enforces
  fine-grained permissions (`Create_Request`, `Update_Request`,
  `Cancel_Request`, `Approve_Request`, `Reject_Request`) rather than
  role names, calls into `workflow/service.py`. Temporal itself has no
  concept of roles or permissions — this is the sole enforcement point
  for that front door. See the Keycloak setup section below for how
  `Operator`/`Manager` map onto these permissions.
- **`bff/`** — the server-rendered HTMX UI (mock session auth, POC only)
  plus `/sandbox/*`, a standalone playground for htmx experiments (no
  auth, no Temporal/Postgres).
- **`app.py`** — assembles the actual FastAPI app: lifespan (Temporal
  client + Postgres pool), session middleware, and mounts all three
  routers (`bff.ui`, `bff.sandbox`, `api.routes`).
- **`db/schema.sql`** — Postgres table for listing/reporting/audit.
  Temporal is the source of truth for *live* state; Postgres is the
  queryable record.

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
`worker-activity`), and the `bff`. No Keycloak, no manual `createdb`, no
manual `pip install`.

Scale worker capacity for a rough local HA test:

```bash
docker compose up --build --scale worker-activity=3
```

- App / UI: **http://localhost:8000** (redirects to `/ui/login`)
- Temporal Web UI: **http://localhost:8233**
- Postgres: `localhost:5432` (`temporal`/`temporal`, databases `temporal`
  and `review_approval`)

`KEYCLOAK_ISSUER` is left unset in `docker-compose.yml` — the JSON API's
Keycloak-protected routes return a 503 if called, but `/ui/*` (mock auth)
works fully without it.

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

#### 3. Keycloak (optional — only needed for the JSON API, not `/ui/*`)

> **Note:** the realm/resources/policies/permissions/users described
> below are real, provisioned, and verified end to end (see below) —
> but `api/auth.py`/`api/routes.py` haven't been updated to check them
> yet, so calls will currently get a `403 requires role: operator`/
> `manager` (a stale, case-sensitive check against the old lowercase
> role names) even with a perfectly valid token. See `CLAUDE.md`'s
> "Known gaps" for status — this isn't a small gap, since the five
> permissions are Resources now, not roles, so a real fix needs a UMA
> ticket exchange, not just fixing the string being checked.

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

- Two plain (non-composite) realm roles: `Operator`, `Manager`
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
- Four demo users, password `password` for all: `operator1`/`operator2`
  (`Operator`), `manager1`/`manager2` (`Manager`)

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
resource-server's own view of what it's allowed to do (this is the part
`api/auth.py` will need to do once `require_permission()` exists — see
`CLAUDE.md`'s "Known gaps"):

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

## POC UI (HTMX, mock auth)

Whichever setup option you used, the UI lives at `/ui/*`, in the same
FastAPI app as the JSON API.

Open **http://localhost:8000** — it redirects to a mock login screen with
four one-click buttons (Operator One, Operator Two, Manager One, Manager
Two) — no password, no username entry, just a plain session cookie.

- **Operator screen** (`/ui/operator`) — shows only *your* requests. "+ New
  Request" opens a dialog to pick a review type and paste a JSON payload.
  Pending requests can be edited or cancelled; decided/cancelled ones are
  view-only.
- **Manager screen** (`/ui/manager`) — shows *all* requests across every
  operator. Clicking a row opens a dialog with the full JSON payload;
  pending ones get Approve/Reject buttons, decided ones are view-only.

Both screens poll every 5 seconds so a Manager's decision shows up on the
Operator's screen (and vice versa) without a manual refresh.

The UI calls `review_approval/workflow/service.py` directly with its own
mock session rather than going through the Keycloak-protected JSON API —
both front doors funnel through the same `service.py` functions, so
there's one source of truth for what's allowed.

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
client:

```bash
TOKEN=$(curl -s -X POST \
  "$KEYCLOAK_ISSUER/protocol/openid-connect/token" \
  -d "client_id=review-approval" \
  -d "client_secret=dev-secret-change-me" \
  -d "grant_type=password" \
  -d "username=operator1" \
  -d "password=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

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
- `review_approval/bff/mock_auth.py` and the `/ui/*` routes are POC-only
  — no password, no identity verification. Replace with a real
  Keycloak-backed session before this goes anywhere beyond a local demo.
- `verify_aud=False` in `review_approval/api/auth.py` — set and verify a
  real audience once the Keycloak client is configured.
- No timeout on "wait for Manager decision" — consider adding a
  `workflow.wait_condition(..., timeout=...)` plus a reminder/escalation
  activity.
- No notification step (email/Slack) on request creation or decision.
- Adding a review type with no worker actually polling its queue leaves
  requests stuck at `PENDING_REVIEW` forever with no error anywhere.
