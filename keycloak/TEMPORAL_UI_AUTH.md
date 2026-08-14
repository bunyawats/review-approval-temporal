# Temporal Web UI: Keycloak-gated login

Real Keycloak login for Temporal Web UI (`temporal-ui` service, port
`8233`), restricted to a dedicated `TemporalAdmin` realm role.
**Authentication only** — see "Known limitations" at the bottom before
assuming this does more than it does.

**For the general mechanism** (how conditional-role-gate authentication
flows work in Keycloak, why `temporalio/ui`'s `PROVIDER_URL`/
`ISSUER_URL` behave the way they do, and every gotcha hit getting this
right) **see the `keycloak-admin` and `temporal-admin` skills** — that
knowledge is written generically there, not repeated here. This file is
just what's specific to *this* repo: exact names/secrets, exact
reproduction steps, and what to verify.

Everything needed is already checked into this repo. The **only** thing
that can't be checked in is one host-machine `/etc/hosts` line — see
step 2 below.

---

## Reproducing this on a fresh machine

1. Clone this repo (the realm config and `docker-compose.yml` changes
   are already in it — no manual Keycloak admin-console clicking, no
   re-running the setup that produced this file).
2. Add this to `/etc/hosts` (needs `sudo`) — **required**, login will
   fail without it (see the `temporal-admin` skill for exactly why —
   short version: `temporalio/ui` bakes `TEMPORAL_AUTH_PROVIDER_URL`
   directly into the browser redirect, so the Docker-internal `keycloak`
   hostname needs to resolve from the browser too):
   ```bash
   echo '127.0.0.1 keycloak' | sudo tee -a /etc/hosts
   ```
3. Bring up the Dockerized stack (this feature does **not** work with
   the native `temporal server start-dev` CLI — its bundled UI has no
   OIDC config surface at all):
   ```bash
   docker compose up -d postgres temporal temporal-ui keycloak
   # or just: docker compose up --build
   ```
4. Open `http://localhost:8233`, log in as `temporal-admin1` / `password`.

## Verifying it worked

- **Positive case**: `http://localhost:8233` → log in as
  `temporal-admin1` / `password` → lands on the real Temporal dashboard.
- **Negative case**: same URL, private/incognito window (so Keycloak's
  own SSO session doesn't leak in) → log in as `operator1` or
  `manager1` / `password` → Keycloak itself shows **"Access denied"**,
  before Temporal UI's own callback ever runs.
- Both were verified live via the real OIDC handshake (`curl` driving
  the actual redirect → Keycloak login form → callback exchange, not
  just config review) — `temporal-admin1` gets a real authorization
  code and a session cookie whose JWT payload decodes to
  `"Name":"Temporal Admin"`; `operator1`/`manager1` both get a genuine
  `401 Access denied` straight from Keycloak.
- If step 1 above fails with the browser unable to reach `keycloak` at
  all, the `/etc/hosts` line is missing or wrong — check with
  `cat /etc/hosts | grep keycloak`.

---

## Why a dedicated `TemporalAdmin` role, not "any Keycloak user"

This app's whole permission model (Operators see only their own
requests — see `CLAUDE.md`'s Invariants section) lives entirely in the
application layer (`workflow/service.py`). Temporal itself has zero
concept of it. Since Temporal Web UI shows *every* workflow execution
and its full payload with no per-user filtering, letting any
Keycloak-authenticated user (including every existing Operator/Manager
demo user) log in would let any Operator see every other Operator's
request payloads directly through Temporal UI — a real way to bypass
the app's own visibility invariant. Restricting login to a role nobody
has by default (`TemporalAdmin`, granted only to `temporal-admin1`
here) closes that off. (Why full server-side per-user authorization
isn't attempted instead — see the `temporal-admin` skill.)

## Exact config used in this repo

Client (`keycloak/import/myrealm-realm.json`), a second confidential
client separate from the app's own `review-approval` client:

```json
{
  "clientId": "temporal-ui",
  "secret": "temporal-ui-dev-secret-change-me",
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "redirectUris": ["http://localhost:8233/auth/sso/callback"],
  "webOrigins": ["http://localhost:8233"],
  "authenticationFlowBindingOverrides": {
    "browser": "b10c3f6e-0000-4000-8000-000000000001"
  }
}
```

Flow structure (see `keycloak/import/myrealm-realm.json`'s
`authenticationFlows` for the full JSON, and the `keycloak-admin` skill
for why it's shaped this way):

```
temporal-ui-browser (top-level, id: b10c3f6e-0000-4000-8000-000000000001)
├─ Username Password Form                          REQUIRED
└─ [subflow] temporal-ui-require-admin-role         CONDITIONAL
    ├─ Condition - user role (TemporalAdmin, negate: true)   REQUIRED
    └─ Deny access                                            REQUIRED
```

`docker-compose.yml`'s `temporal-ui` service:

```yaml
TEMPORAL_AUTH_ENABLED: true
TEMPORAL_AUTH_PROVIDER_URL: http://keycloak:8080/realms/myrealm
TEMPORAL_AUTH_CLIENT_ID: temporal-ui
TEMPORAL_AUTH_CLIENT_SECRET: temporal-ui-dev-secret-change-me
TEMPORAL_AUTH_CALLBACK_URL: http://localhost:8233/auth/sso/callback
TEMPORAL_AUTH_SCOPES: openid,profile,email
```

Deliberately **no** `TEMPORAL_AUTH_ISSUER_URL` — the `/etc/hosts` fix
above makes `PROVIDER_URL` and the token's real `iss` claim agree on
their own, so it's left at its default (see the `temporal-admin` skill
for why setting it to a different host than `PROVIDER_URL` actively
breaks login rather than helping).

---

## Known limitations

- **Authentication only.** Every `TemporalAdmin` sees every workflow's
  full payload, unfiltered by requester — there is no per-user
  filtering inside Temporal itself.
- **Docker Compose only.** The native `temporal server start-dev` CLI's
  bundled UI has no OIDC config surface — this only applies when
  `temporal`/`temporal-ui` run via `docker-compose.yml`.
- **`/etc/hosts` is machine-local and manual.** It's not something
  `docker-compose.yml` can set up for you; every machine that wants to
  use this needs the one-line addition documented above.
- Secrets (`temporal-ui-dev-secret-change-me`) are plainly checked into
  `keycloak/import/myrealm-realm.json`, same as `review-approval`'s —
  fine for `start-dev`-only local dev, never do this anywhere real.

## Files touched

- `keycloak/import/myrealm-realm.json` — `TemporalAdmin` role,
  `temporal-ui` client, `temporal-ui-browser` +
  `temporal-ui-require-admin-role` authentication flows,
  `temporal-admin1` demo user.
- `docker-compose.yml` — `temporal-ui` service's `TEMPORAL_AUTH_*` env
  vars.
- `/etc/hosts` (manual, machine-local, not in this repo).
