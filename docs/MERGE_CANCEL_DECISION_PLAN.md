# Merge cancel into decision

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (same convention as `docs/PAGINATION_PLAN.md`,
> `docs/SESSION_STORE_PLAN.md`, `docs/BULK_ACTIONS_PLAN.md`,
> `docs/SELECT_ALL_CHECKBOX_PLAN.md`).
>
> - [x] Phase 1 — `workflow/workflows.py`: fold `cancel_request` into
>       `submit_decision`; `VALID_DECISIONS` gains `"CANCELLED"`
> - [x] Phase 2 — `workflow/activities.py`: fold `persist_cancel` into
>       `persist_decision`; delete `PersistCancelInput`/`persist_cancel`
> - [x] Phase 3 — `workflow/worker.py`: update the registered-activities
>       list to match Phase 2
> - [x] Phase 4 — `workflow/service.py`: fold `cancel_review()` into
>       `submit_decision()`, `bulk_cancel_reviews()` into
>       `bulk_submit_decision()`; conditional ownership check by
>       decision type
> - [x] Phase 5 — `api/routes.py`: remove `/reviews/{id}/cancel` and
>       `/reviews/bulk/cancel`; `/reviews/{id}/decision` and
>       `/reviews/bulk/decision` gain 3-way permission branching and a
>       `PermissionError` → 403 handler
> - [x] Phase 6 — `bff/ui.py` + templates: remove
>       `cancel-form`/`cancel`/`bulk-cancel-form`/`bulk-cancel` operator
>       routes and `_cancel_dialog.html`; fold Cancel into
>       `_detail_dialog.html` (operator variant) and rename
>       `bulk-cancel-form`/`bulk-cancel` to
>       `bulk-decision-form`/`bulk-decision` on the operator side too;
>       **drop the `require_session_role("manager")` pre-gate from the
>       merged manager decision route** (single-item and bulk) — the
>       real Keycloak permission check is sufficient on its own, same as
>       the REST API already relies on with no role gate at all;
>       `require_session_role()` stays only on `GET` page/detail routes
> - [x] Phase 7 — tests: update every test referencing the removed
>       routes/functions/templates across `tests/unit/` and
>       `tests/integration/`
> - [x] Phase 8 — docs: `CLAUDE.md` (extensive — this touches nearly
>       every bullet describing the cancel/decision split),
>       `docs/BULK_ACTIONS_PLAN.md` (status-tracker note), Keycloak
>       docs unchanged (see "Decisions already made")
>
> **All 8 phases complete.** Full test suite (95 tests) passes, `ruff
> check` clean across `review_approval/` and `tests/`. Confirmed with
> the user before writing this plan: full
> consolidation, not a thin-alias approach — `/reviews/{id}/cancel` and
> `/reviews/bulk/cancel` are removed entirely, not kept as
> backward-compatible wrappers. This is a breaking REST API change,
> accepted deliberately (see "Decisions already made"). Also confirmed:
> permission enforcement must be scope-based only, never role-based, for
> any of the five mutating actions — this refactor is also the moment to
> drop `manager_decision`'s `require_session_role("manager")` pre-gate,
> not just carry it forward onto the merged route (see "Decisions
> already made").

## Decisions already made (don't re-litigate without new information)

- **Full consolidation, not a thin alias.** `POST /reviews/{id}/cancel`
  and `POST /reviews/bulk/cancel` are removed outright.
  `POST /reviews/{id}/decision` / `POST /reviews/bulk/decision` become
  the only mutating endpoints for all three terminal outcomes, taking
  `decision: "APPROVED" | "REJECTED" | "CANCELLED"`. This is a genuine
  breaking change to the REST API's URL surface — accepted deliberately
  (POC stage, no external consumers to preserve compatibility for).
- **Keycloak config does not change.** `Cancel`/`Approve`/`Reject` stay
  three independent Scopes on the `RequestApproval` Resource, gated by
  `Operator Policy`/`Manager Policy` respectively (confirmed against the
  live `keycloak/import/myrealm-realm.json` — unchanged by this refactor).
  What changes is purely which scope name the merged code picks, based on
  the `decision` value — the same pattern `submit_decision`'s route
  already uses for Approve vs Reject, just extended to a 3-way branch
  (`decision == "CANCELLED" → "Cancel"`, `"APPROVED" → "Approve"`,
  `"REJECTED" → "Reject"`).
- **Ownership semantics per decision type stay exactly as they are
  today — this is the "handle scope permission carefully" part.** They
  don't unify into one rule, because they were never the same rule:
  - `decision == "CANCELLED"`: the acting user must be the review's own
    `requester` (`PermissionError` otherwise) — unchanged from today's
    `cancel_review()`.
  - `decision in ("APPROVED", "REJECTED")`: no ownership check — any
    user holding the `Approve`/`Reject` permission may decide, same as
    today's `submit_decision()`. A manager deciding on their own
    request (if a manager ever also had a request of their own) is
    still allowed, exactly as today.
  - Both still require `status == "PENDING_REVIEW"` first — unchanged,
    and checked before the ownership branch so a wrong-requester cancel
    attempt on an already-terminal row still gets the "no longer
    editable"-style `ValueError`, not a `PermissionError`, matching
    today's `cancel_review()` ordering.
- **`workflow/workflows.py`'s native-Temporal-cancel recovery (the
  `except asyncio.CancelledError` block) and `service.py`'s
  `_reconcile_missing_workflow()` (deleted-workflow recovery) are
  *not* part of this merge and don't change in kind** — they're
  separate mechanisms from the app's own cancel/decision signal path
  (one catches a Temporal-native cancel with no signal involved at
  all, the other bypasses Temporal/activities entirely). They do need
  small mechanical updates where they currently reference the
  soon-removed `persist_cancel`/`PersistCancelInput` (see Phase 1/2
  below), but their own logic, triggers, and the invariants
  `CLAUDE.md` documents for them are unaffected.
- **BFF dialogs merge along with the routes.** The operator's dedicated
  `_cancel_dialog.html` (a small "Cancel this request?" confirmation)
  is removed; cancelling becomes part of `_detail_dialog.html` — the
  same template the manager's Approve/Reject flow already uses — with
  a Cancel button shown when `role == "operator"` and the record is
  still `PENDING_REVIEW` and `"Cancel"` is in the caller's permissions
  (mirroring exactly how Approve/Reject are already conditionally shown
  for `role == "manager"`). This means `operator_detail()` needs to
  start passing `permissions` into the template (it currently doesn't,
  since operator's view never needed a permission-gated button before).
  The *route* surface for operator and manager stays separate
  (`/ui/operator/...` vs `/ui/manager/...`, each still gated by its own
  `require_session_role(...)`) — only the underlying service call and,
  for the single-item flow, the dialog template unify. Operator's
  decision route never accepts a client-submitted `decision` value; it
  hardcodes `"CANCELLED"` server-side, since an operator session can
  never legitimately submit anything else — no 3-way branch needed on
  that side, only on the REST API's single shared endpoint.
- **Bulk follows the same shape as single-item.** Operator's
  `bulk-cancel-form`/`bulk-cancel` routes are renamed to
  `bulk-decision-form`/`bulk-decision` (mirroring manager's existing
  naming), calling the merged `service.bulk_submit_decision()` with
  `decision="CANCELLED"` hardcoded server-side. `_bulk_confirm_dialog.html`
  and `_bulk_result_dialog.html` need no changes — they're already
  shared/generic across all three actions (built that way in
  `docs/BULK_ACTIONS_PLAN.md`'s original design).
- **One shared `VALID_DECISIONS` tuple, not two independently-maintained
  copies.** `workflow/workflows.py` already defines
  `VALID_DECISIONS = ("APPROVED", "REJECTED")`; `service.py` currently
  hardcodes its own `if decision not in ("APPROVED", "REJECTED")` check
  separately. Post-merge, `service.py` imports and reuses the same
  tuple (now `("APPROVED", "REJECTED", "CANCELLED")`) instead of
  hardcoding a second copy that could drift.
- **Permission enforcement must be scope-based only, never role-based —
  audited against the actual code (not memory/CLAUDE.md summary) before
  writing this bullet.** Confirmed:
  - `api/auth.py` (REST API): 100% scope-based. `require_permission()`/
    `check_permission()` do a real Keycloak UMA ticket exchange
    checking Create/Update/Cancel/Approve/Reject. No role check
    anywhere in `api/routes.py`.
  - `workflow/service.py`: zero permission *or* role logic (grepped
    directly — no hits). Only ownership (`record["requester"]`) and
    status (`PENDING_REVIEW`) checks, which are business rules, not
    Keycloak authorization.
  - `bff/keycloak_session.py`: two genuinely different mechanisms.
    `require_session_role(role)` is a real role check (`Operator`/
    `Manager` realm roles) — legitimate for gating *screen* access
    (`GET /ui/operator` vs `GET /ui/manager`, and the `GET .../detail`
    view routes, since "view" isn't a Keycloak scope at all). Separately,
    `require_permission()`/`check_permission()` do the same real UMA
    check as the REST API, for the 5 mutating actions.
  - **The one place these were mixed**: `manager_decision` is currently
    gated by *both* `Depends(require_session_role("manager"))` **and**
    the inline `check_permission()` — a role check layered onto a
    mutating action. `CLAUDE.md` documented this dual-gate as
    deliberate ("a real behavioral difference from the REST API, not a
    bug"). **This refactor removes it.** The merged manager decision
    route (single-item and bulk) drops the `require_session_role()`
    dependency and relies solely on `check_permission()`, exactly like
    the REST API and like the operator side already does (operator's
    decision route was never role-gated to begin with — only
    `require_permission("Cancel")`).
  - **This is safe, not just consistent** — verified by reasoning
    through the failure mode, not assumed: without the role gate, an
    operator session hitting `/ui/manager/{id}/decision` still gets
    blocked, because `check_permission(user, "Approve"/"Reject")` fails
    on its own (an operator's token was never granted those scopes).
    The only observable change is the 403's message: `"requires role:
    manager"` becomes `"requires permission: Approve"` (or `Reject`) —
    arguably *more* accurate, and now consistent with how the REST API
    has always reported the equivalent failure. Any existing test
    asserting the old role-based message text needs updating, not just
    moving (see "Test migration" below).
  - `GET /ui/operator/{id}/detail` and `GET /ui/manager/{id}/detail`
    (viewing, not deciding) keep their existing `require_session_role()`
    gates unchanged — viewing isn't one of the five Keycloak scopes, so
    role-gating remains the only mechanism available for screen/detail
    access, consistent with "role gates screens, not the five mutating
    actions."

## Design

### `workflow/workflows.py`

```python
VALID_DECISIONS = ("APPROVED", "REJECTED", "CANCELLED")

@workflow.signal
async def submit_decision(self, decision: str, actor: str, comment: str = "") -> None:
    if not self._claim_final():
        return
    if decision not in VALID_DECISIONS:
        self._closing = False
        raise ApplicationError(f"invalid decision: {decision}")
    await workflow.execute_activity(
        persist_decision,
        PersistDecisionInput(
            request_id=self._request_id,
            decision=decision,
            closed_by=actor,
            closed_comment=comment,
        ),
        start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
        retry_policy=DEFAULT_RETRY_POLICY,
    )
    self._closed_by = actor
    self._closed_comment = comment
    self._status = decision
    self._decision_received = True
```

`cancel_request` signal is deleted entirely — its old body is now just
the `decision == "CANCELLED"` case of the same handler above. The
`manager_id` param is renamed `actor` (a cancelling operator isn't a
manager) — this is a signal-signature rename, so double check nothing
else references the old keyword name positionally-only calls are
already used everywhere (`[decision, actor, comment]` args list in
`service.py`, so this is safe).

The `except asyncio.CancelledError` block in `run()` (native Temporal
cancel recovery) still calls the activity **directly**, not through this
signal — it now targets the merged `persist_decision` activity instead
of `persist_cancel`:

```python
except asyncio.CancelledError:
    if self._claim_final():
        await workflow.execute_activity(
            persist_decision,
            PersistDecisionInput(
                request_id=self._request_id,
                decision="CANCELLED",
                closed_by="temporal-admin",
                closed_comment="forced by temporal system",
                closed_at=workflow.now(),
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._cancelled = True
        self._status = "CANCELLED"
        self._closed_by = "temporal-admin"
        self._closed_comment = "forced by temporal system"
```
This requires `PersistDecisionInput` to gain the optional `closed_at`
field `PersistCancelInput` used to carry (see Phase 2) — nothing else
about this block's behavior/invariants changes.

**Operational risk worth flagging, not solving now (POC-acceptable):**
removing a `@workflow.signal` handler is not zero-cost for any workflow
execution still open (`PENDING_REVIEW`) at deploy time. Temporal signal
dispatch is by name at the time the signal arrives — if a front door
still running old code sends a `cancel_request` signal to a worker
already running this new code, the worker has no handler for that name.
Locally this is moot (Temporal dev server is in-memory and gets wiped
routinely — see `docs/SELECT_ALL_CHECKBOX_PLAN.md`'s cleanup precedent),
but a real deployment would need worker and front-door pods rolled
together, or a transition window where the old signal name is kept as a
deprecated shim that just calls into the same merged logic. Not doing
that here — noting it so it isn't forgotten if this app ever needs a
real production deploy story.

### `workflow/activities.py`

```python
@dataclass
class PersistDecisionInput:
    request_id: str
    decision: str  # APPROVED | REJECTED | CANCELLED
    closed_by: str
    closed_comment: str
    closed_at: Optional[datetime] = None  # moved from PersistCancelInput


@activity.defn
async def persist_decision(inp: PersistDecisionInput) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE review_requests
            SET status = $2,
                closed_by = $3,
                closed_comment = $4,
                closed_at = COALESCE($5, now())
            WHERE id = $1
            """,
            inp.request_id,
            inp.decision,
            inp.closed_by,
            inp.closed_comment,
            inp.closed_at,
        )
```
`PersistCancelInput`/`persist_cancel` are deleted. The merged activity's
SQL is a strict superset of both originals (`persist_decision` always
passed `closed_at=None` implicitly via `now()`; folding in
`COALESCE($5, now())` preserves that exactly while adding
`persist_cancel`'s optional-override behavior).

### `workflow/worker.py`

Update the activity registration list: remove `persist_cancel`, keep
`persist_decision` (now importable with its expanded `PersistDecisionInput`).
Also update `workflows.py`'s `imports_passed_through()` block the same way.

### `workflow/service.py`

```python
from review_approval.workflow.workflows import VALID_DECISIONS, ReviewApprovalWorkflow, ReviewRequestInput

async def submit_decision(
    client: Client,
    pool: asyncpg.Pool,
    request_id: str,
    decision: str,
    actor: str,
    comment: str = "",
) -> None:
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_DECISIONS}")
    record = await get_review(pool, request_id)
    if record is None:
        raise LookupError("review not found")
    if record["status"] != "PENDING_REVIEW":
        raise ValueError("this review has already been decided")
    if decision == "CANCELLED" and record["requester"] != actor:
        raise PermissionError("only the requester who created this review can cancel it")
    handle = client.get_workflow_handle(workflow_id(request_id))
    await _signal_or_reconcile(
        pool, handle, request_id, ReviewApprovalWorkflow.submit_decision, [decision, actor, comment]
    )
    await _wait_until(pool, request_id, lambda record: record["status"] == decision)
```

`cancel_review()` is deleted; every call site becomes
`submit_decision(client, pool, request_id, "CANCELLED", requester, comment)`.
Note the check ordering — status is checked **before** the ownership
branch, matching today's `cancel_review()` (LookupError → ValueError →
PermissionError), so a wrong-requester cancel attempt on an
already-terminal row still surfaces "already been decided", not a
permission error, exactly as it would today.

```python
async def bulk_submit_decision(
    client: Client,
    pool: asyncpg.Pool,
    request_ids: list[str],
    decision: str,
    actor: str,
    comment: str = "",
) -> list[BulkActionResult]:
    ids = _validate_bulk_ids(request_ids)
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_DECISIONS}")

    async def _one(request_id: str) -> BulkActionResult:
        try:
            await submit_decision(client, pool, request_id, decision, actor, comment)
            return BulkActionResult(request_id, True)
        except (LookupError, PermissionError, ValueError) as e:
            return BulkActionResult(request_id, False, str(e))

    return list(await asyncio.gather(*(_one(rid) for rid in ids)))
```
`bulk_cancel_reviews()` is deleted. Note `_one()`'s except clause now
needs `PermissionError` added (it wasn't needed before since bulk
decision was Approve/Reject-only) — a stale per-item ownership mismatch
should surface as a per-item failure, not blow up the whole batch, same
"best-effort, per-item results" semantics `docs/BULK_ACTIONS_PLAN.md`
already established.

### `api/routes.py`

```
POST /reviews/{id}/decision
  body: {"decision": "APPROVED"|"REJECTED"|"CANCELLED", "comment": ""}
  permission = {"APPROVED": "Approve", "REJECTED": "Reject", "CANCELLED": "Cancel"}[body.decision]
  -> check_permission(user, permission)
  -> service.submit_decision(client, pool, id, body.decision, user["sub"], comment)
  -> except LookupError: 404
  -> except PermissionError as e: 403   # NEW -- CANCELLED can now raise this
  -> except ValueError as e: 400

POST /reviews/bulk/decision
  body: {"request_ids": [...], "decision": "APPROVED"|"REJECTED"|"CANCELLED", "comment": ""}
  same 3-way permission lookup as above
  -> service.bulk_submit_decision(...)
```
`POST /reviews/{id}/cancel`, `POST /reviews/bulk/cancel`,
`CancelRequest`, `BulkCancelRequest` are all deleted. **The
`except PermissionError` branch on `/decision` is new and easy to
forget** — today's `/decision` route never needed it (Approve/Reject
never raised `PermissionError`); omitting it here would turn a
wrong-requester cancel attempt into an unhandled 500 instead of a 403.
The route-registration-order concern that mattered for
`/reviews/bulk/cancel` vs `/reviews/{id}/cancel` goes away with this
merge (only `/reviews/bulk/decision` vs `/reviews/{id}/decision`
remains, already correctly ordered today).

### `bff/ui.py` + templates

**Operator, single item:**
```
GET  /ui/operator/{id}/detail          (replaces .../cancel-form)
  -> require_session_role("operator") [+ 404 if not own request, unchanged]
  -> now also computes permissions = await _user_permissions(user)
  -> renders _detail_dialog.html with role="operator", permissions=permissions

POST /ui/operator/{id}/decision        (replaces .../cancel)
  -> require_permission("Cancel")
  -> service.submit_decision(client, pool, id, "CANCELLED", user["username"], comment)
     # decision is hardcoded here, never read from the request body
  -> _operator_row_response.html, same as today's cancel route
```
`cancel_form`/`cancel_request_route` are deleted;
`operator_detail()` gains the `permissions` computation.
`_cancel_dialog.html` is deleted.

**`_detail_dialog.html`** gains a Cancel branch parallel to its existing
Approve/Reject branch:
```jinja
{% set can_cancel = role == "operator" and record.status == "PENDING_REVIEW" and "Cancel" in permissions %}
...
{% if can_cancel %}
<textarea id="cancel-comment-{{ record.id }}">...</textarea>
<button onclick="... htmx.ajax('POST', '/ui/operator/{{ record.id }}/decision',
  {source: ..., target: '#row-{{ record.id }}', select: 'tr', swap: 'outerHTML swap:400ms',
  values: {comment: c}})">Cancel Request</button>
{% endif %}
```
(Exact button styling/copy — "Confirm Cancel" vs "Cancel Request", red
vs the existing red — carried over from `_cancel_dialog.html`, not
redesigned; this is a consolidation, not a visual redesign.)

**Operator, bulk:**
```
POST /ui/operator/bulk-decision-form   (replaces .../bulk-cancel-form)
  -> require_permission("Cancel")
  -> ids = _get_selection(...); items = service.get_reviews(...) filtered to own requester
  -> renders _bulk_confirm_dialog.html(action="Cancel", decision=None, post_url="/ui/operator/bulk-decision", role="operator")

POST /ui/operator/bulk-decision        (replaces .../bulk-cancel)
  -> require_permission("Cancel")
  -> service.bulk_submit_decision(client, pool, ids, "CANCELLED", user["username"], comment)
     # decision hardcoded, never read from the request body
  -> _bulk_result_response(..., role="operator", ...)
```
`bulk_cancel_form`/`bulk_cancel_execute` are deleted; the operator
toolbar's "Bulk Cancel" button (`_operator_bulk_toolbar.html`) now posts
to `/ui/operator/bulk-decision-form` instead of `/ui/operator/bulk-cancel-form`.

**Manager routes stay Approve/Reject-only in what they accept** (still
their own existing 2-way permission branch, now calling into a
`service.submit_decision()`/`bulk_submit_decision()` that also happens
to accept a third decision value they never send) — **but their gating
does change**: `manager_decision` (and the manager bulk-decision
execute route) drop `Depends(require_session_role("manager"))` and rely
solely on the inline `check_permission()` call, per the permission-
architecture decision above. `manager_detail`/the manager
bulk-decision-*form* (dialog-open) routes are unaffected — they were
never mutating actions, so they keep `require_session_role("manager")`
as their only gate, same as today.

```
POST /ui/manager/{id}/decision
  decision: str = Form(...)  # APPROVED | REJECTED (client-submitted, unchanged)
  comment: str = Form("")
  user: dict = Depends(get_session_user)   # was Depends(require_session_role("manager"))
  -> permission = "Approve" if decision == "APPROVED" else "Reject"
  -> await check_permission(user, permission)   # unchanged, now the *only* gate
  -> service.submit_decision(client, pool, id, decision, user["username"], comment)
```
(`get_session_user` still applies — a route can't be reached with no
session at all — it's specifically the *role* check being dropped, not
authentication itself.)

```
POST /ui/manager/bulk-decision
  decision: str = Form(...)  # APPROVED | REJECTED, unchanged
  comment, page, query_id: unchanged
  user: dict = Depends(get_session_user)   # was Depends(require_session_role("manager"))
  -> permission = "Approve" if decision == "APPROVED" else "Reject"
  -> await check_permission(user, permission)   # unchanged, now the *only* gate
  -> service.bulk_submit_decision(client, pool, ids, decision, user["username"], comment)

POST /ui/manager/bulk-decision-form   (dialog preview, non-mutating -- unaffected)
  user: dict = Depends(require_session_role("manager"))   # unchanged
```

## Test migration (Phase 7)

Every reference to the removed surface needs updating, not just
deleting — replace with the equivalent `decision="CANCELLED"` call/route:

- `tests/unit/test_service_bulk.py` — tests monkeypatch
  `service.cancel_review`/`service.submit_decision` separately; collapse
  to monkeypatching the single merged `service.submit_decision`, add a
  case covering the `PermissionError` per-item path (not previously
  exercised for bulk decision, only for bulk cancel).
- `tests/integration/test_api_permissions.py` — `/reviews/{id}/cancel`
  tests move to `/reviews/{id}/decision` with `decision: "CANCELLED"`.
- `tests/integration/test_api_bulk.py` — same for
  `/reviews/bulk/cancel` → `/reviews/bulk/decision`.
- `tests/integration/test_bff_permissions.py` — `/ui/operator/{id}/cancel*`
  tests move to `/ui/operator/{id}/detail` (dialog) +
  `/ui/operator/{id}/decision` (execute). **`test_operator_cannot_reach_
  manager_decision` needs its assertion text changed, not just moved** —
  it currently asserts `"requires role: manager" in
  response.json()["detail"]`; per the dropped role-gate decision above,
  an operator hitting `/ui/manager/{id}/decision` now gets `403
  "requires permission: Approve"` (or `Reject`, depending on which
  decision the test submits) instead. Don't let this test keep passing
  by accident against the old message string — it's exactly the kind of
  assertion that silently stops testing what it claims to if left
  untouched.
- `tests/integration/test_bff_bulk.py` — `/ui/operator/bulk-cancel*`
  tests move to `/ui/operator/bulk-decision*`; `_create_as`/`_select`
  helpers are unaffected. Same message-text check applies to any bulk
  test asserting the old role-based 403 on the manager bulk-decision
  execute route, if one exists.

**Full permission-branch matrix for `/reviews/{id}/decision` and
`/reviews/bulk/decision` post-merge** — the 3-way `check_permission()`
lookup (`APPROVED→Approve`, `REJECTED→Reject`, `CANCELLED→Cancel`) needs
every cell of this covered, not just the diagonal:

| Actor holds → tries decision ↓ | Operator (`Cancel` only) | Manager (`Approve`+`Reject`) |
|---|---|---|
| `CANCELLED` | 200 (if also the requester) | **403 — new case, not reachable before this merge** (decision used to be Approve/Reject-only) |
| `APPROVED` | **403 — pre-existing test, must carry forward unchanged**: `test_operator_cannot_approve_or_reject` (single) and its bulk counterpart in `test_api_bulk.py` already cover this against today's Approve/Reject-only endpoint; re-verify both still pass unmodified once `decision` also accepts `CANCELLED`, since a regression here would be an operator silently gaining approve/reject rights, not just a missing test | 200 |
| `REJECTED` | same 403, same pre-existing test | 200 |

The right-hand new case (manager → `CANCELLED`) is the only genuinely
*new* test to write. The left-hand cases already exist and are exactly
the ones a sloppy 3-way branch (e.g. a typo'd lookup dict, or an `if
decision == "CANCELLED": permission = "Cancel"` `else` that defaults
too permissively) would silently break — don't skip re-running them
just because they're "old" tests.

## Docs (Phase 8)

`CLAUDE.md` needs the heaviest doc pass of any refactor this session —
it documents the current split in detail across the `workflows.py`,
`activities.py`, `service.py`, `api/routes.py`, `bff/ui.py`,
`bff/templates/` bullets and the "Invariants" section. Read each of
those bullets fresh while implementing each phase above (don't try to
patch them from memory of this plan) — in particular the long
native-Temporal-cancel paragraph and `_claim_final()` paragraph
reference `persist_cancel`/`cancel_request` by name and need updating
to the merged names without losing any of the reasoning they currently
capture. `docs/BULK_ACTIONS_PLAN.md`'s status-tracker note should get a
short addendum pointing here, same as `docs/SELECT_ALL_CHECKBOX_PLAN.md`
does for the select-all redesign.

**Specifically don't miss**: the `bff/ui.py`/`bff/keycloak_session.py`
bullets currently describe `manager_decision`'s dual role+permission
gate as deliberate, permanent, intentionally-asymmetric-vs-the-REST-API
behavior ("a real behavioral difference between the two front doors,
not a bug"). That characterization is now wrong and needs rewriting,
not just a patch — the dual gate is removed (see "Decisions already
made" above), and the REST API/BFF asymmetry it used to describe no
longer exists for the merged decision routes. Leaving the old
"deliberate, not a bug" framing in place after removing the thing it
was defending would actively mislead the next reader.
