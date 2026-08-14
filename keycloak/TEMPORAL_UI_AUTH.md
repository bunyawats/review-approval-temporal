# Temporal Web UI: Keycloak-gated login

Real Keycloak login for Temporal Web UI (`temporal-ui` service, port
`8233`), restricted to a dedicated `TemporalAdmin` realm role.
**Authentication only** — see "Known limitations" at the bottom before
assuming this does more than it does.

Everything needed is already checked into this repo. The **only** thing
that can't be checked in is one host-machine `/etc/hosts` line — see
"Reproducing this on a fresh machine" below.

---

## Reproducing this on a fresh machine

1. Clone this repo (the realm config and `docker-compose.yml` changes
   are already in it — no manual Keycloak admin-console clicking, no
   re-running the setup that produced this file).
2. Add this to `/etc/hosts` (needs `sudo`) — **required**, login will
   fail without it, see "Why `/etc/hosts`" below for exactly why:
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
- Both were verified live this session via the real OIDC handshake
  (`curl` driving the actual redirect → Keycloak login form → callback
  exchange, not just config review) — `temporal-admin1` gets a real
  authorization code and a session cookie whose JWT payload decodes to
  `"Name":"Temporal Admin"`; `operator1`/`manager1` both get a genuine
  `401 Access denied` straight from Keycloak.
- If step 1 above fails with the browser unable to reach `keycloak` at
  all, the `/etc/hosts` line is missing or wrong — check with
  `cat /etc/hosts | grep keycloak`.

---

## Architecture

### Why authentication-only, not full per-user authorization

Temporal Server supports a pluggable `Authorizer` + `ClaimMapper` for
real per-user RBAC (different roles seeing different namespaces/
actions), but per the [official self-hosted security
docs](https://docs.temporal.io/self-hosted-guide/security), activating
it requires **custom Go server code**
(`temporal.WithAuthorizer()`/`temporal.WithClaimMapper()`) — not env
vars on the stock `temporalio/auto-setup` image. Without an explicit
`Authorizer`, Temporal falls back to `noopAuthorizer`, which allows
*every* request regardless of who's authenticated.

A blog post surfaced while researching this claimed env-var-only
server-side JWT authorization works on the stock image
(`TEMPORAL_JWT_KEY_SOURCE1`, `TEMPORAL_AUTH_CLAIM_MAPPER=default`), but
that contradicts the official docs and couldn't be verified against an
authoritative source — **don't build on it without testing against a
real server first.**

So this implementation deliberately stops at the UI login gate
(`temporalio/ui`'s own `TEMPORAL_AUTH_*` env vars) — real, well
documented, works with the stock image. Full server-side authorization
would be a separate, larger effort.

### Why a dedicated `TemporalAdmin` role, not "any Keycloak user"

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
here) closes that off.

### The Keycloak client: `temporal-ui`

A second confidential client (`keycloak/import/myrealm-realm.json`),
separate from the app's own `review-approval` client:

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

Confidential (needs a secret) because `temporalio/ui` does a real
server-side Authorization Code exchange, same reasoning as
`review-approval`. `authenticationFlowBindingOverrides.browser` points
at the custom flow below **by id**, not alias — Keycloak's binding
mechanism resolves flows by id, so the flow's `id` field and this value
must match exactly (see the flow definition below).

### The conditional authentication flow

Keycloak does **not** gate client login by role out of the box — roles
are just claims on the issued token, not an authentication-time filter.
Restricting who can even log in needed a custom flow:

```
temporal-ui-browser (top-level, id: b10c3f6e-0000-4000-8000-000000000001)
├─ Username Password Form                          REQUIRED
└─ [subflow] temporal-ui-require-admin-role         CONDITIONAL
    ├─ Condition - user role (TemporalAdmin, negate: true)   REQUIRED
    └─ Deny access                                            REQUIRED
```

Deliberately has **no** Cookie/SSO-reuse step (unlike Keycloak's
built-in `browser` flow) — logging into `review-approval` first and
then visiting Temporal UI must still hit the role check for real, not
silently succeed via an existing Keycloak SSO session.

Reading the logic: `Condition - user role` with `negate: true`
evaluates **true** when the user does **not** have `TemporalAdmin`. When
true, the subflow's remaining step (`Deny access`) runs, denying login.
When the user *does* have the role, the condition is false, the
`CONDITIONAL` subflow is skipped entirely (not "failed" — skipped, so
the outer flow just continues to a normal successful login).

### `docker-compose.yml` env vars

```yaml
TEMPORAL_AUTH_ENABLED: true
TEMPORAL_AUTH_PROVIDER_URL: http://keycloak:8080/realms/myrealm
TEMPORAL_AUTH_CLIENT_ID: temporal-ui
TEMPORAL_AUTH_CLIENT_SECRET: temporal-ui-dev-secret-change-me
TEMPORAL_AUTH_CALLBACK_URL: http://localhost:8233/auth/sso/callback
TEMPORAL_AUTH_SCOPES: openid,profile,email
```

Note **no** `TEMPORAL_AUTH_ISSUER_URL` — see below for why that's
deliberate, not an oversight.

### Why `/etc/hosts`, in detail

`temporalio/ui` builds its browser-facing redirect **directly from
`TEMPORAL_AUTH_PROVIDER_URL`** — confirmed live by hitting
`GET /auth/sso` directly and reading the `Location` header, which
echoed back the raw `http://keycloak:8080/...` value. There is no
separate "browser-facing" override env var the way this app's own
`bff` service has in its own Python code (`bff/keycloak_session.py`
constructs the browser-facing URL itself, deliberately different from
the Docker-internal `KEYCLOAK_ISSUER` it uses server-side). Since
`temporalio/ui` is a third-party binary, we don't get to write that
split logic ourselves.

An initial attempt set `TEMPORAL_AUTH_ISSUER_URL=http://localhost:8080/realms/myrealm`
to try to force the browser-facing value separately. That does **not**
work: `ISSUER_URL` is a *different* check entirely — it's compared
against the `iss` claim inside the token Keycloak actually returns.
Since the token was obtained via the `keycloak:8080` path (matching
`PROVIDER_URL`), its `iss` claim says `http://keycloak:8080/...` too —
setting `ISSUER_URL` to a *different* host than that produced a hard
callback failure: `500 Unable to verify ID Token: oidc: id token issued
by a different provider, expected "http://localhost:8080/realms/myrealm"
got "http://keycloak:8080/realms/myrealm"` (confirmed via
`docker compose logs temporal-ui`).

The fix: make `keycloak` resolve to the same place from **both**
inside the Docker network (already true — Docker's internal DNS
resolves service names) **and** from the host/browser. A
`127.0.0.1 keycloak` entry in `/etc/hosts` does exactly that — after
adding it, `PROVIDER_URL` and the token's real `iss` claim agree on
their own, so `TEMPORAL_AUTH_ISSUER_URL` isn't set at all (its
documented default is to fall back to `PROVIDER_URL`).

**An alternative considered and rejected**: setting Keycloak's own
`KC_HOSTNAME=localhost` so it always advertises `localhost:8080`
regardless of which hostname a request came in on. This would avoid
the `/etc/hosts` step, but changes Keycloak's config *realm-wide* —
risking the already-tested `bff`/`api` Keycloak integration (35 passing
tests), which relies on `KEYCLOAK_ISSUER=http://keycloak:8080/...`
server-side. Not worth the blast radius for this feature; the
`/etc/hosts` approach is scoped to just this one problem.

---

## Gotchas hit while building this (reference for future changes to the flow)

These are already fixed in the checked-in realm JSON — listed here only
so a future edit to this flow doesn't reintroduce them blind:

1. **`deny-access` is not a real authenticator id in Keycloak 26.0.8.**
   The correct id is `deny-access-authenticator`. Using the wrong one
   doesn't fail at import time (Keycloak's import doesn't validate
   authenticator ids against the live registry) — it fails at
   *login* time with `RuntimeException: Unable to find factory for
   AuthenticatorFactory: deny-access`, visible only in
   `docker compose logs keycloak`. Verified the correct id via the
   Admin REST API: `GET /admin/realms/{realm}/authentication/authenticator-providers`.
2. **The subflow-referencing execution's `requirement` must be
   `CONDITIONAL` at the parent level, not `REQUIRED`.** With
   `REQUIRED`, a false condition is treated as a hard failure of the
   whole flow — denying *everyone*, including users who have the role
   — regardless of the `negate` setting on the condition itself.
   `CONDITIONAL` is what gives the real "skip this subflow entirely if
   the condition doesn't hold" semantics (the same pattern Keycloak's
   own built-in "Browser - Conditional OTP" subflow uses).
3. **`conditional-user-role` needs `negate: true` here.** Its default
   (`negate` unset/false) evaluates true when the user *has* the role
   — which, combined with `Deny access` as the next step, denies
   exactly the users who should be *allowed*. Verified the exact config
   schema via `GET /admin/realms/{realm}/authentication/config-description/conditional-user-role`.
   All three of these were caught only by testing live against the
   real running Keycloak container and reading its logs — not
   something reading the JSON or the docs alone would have caught.

---

## Known limitations

- **Authentication only.** Every `TemporalAdmin` sees every workflow's
  full payload, unfiltered by requester — there is no per-user
  filtering inside Temporal itself. See "Why authentication-only,
  not full per-user authorization" above.
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
