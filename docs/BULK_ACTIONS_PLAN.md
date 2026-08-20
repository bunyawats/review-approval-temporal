# Bulk cancel / approve / reject

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (same convention as `docs/PAGINATION_PLAN.md` and
> `docs/SESSION_STORE_PLAN.md`).
>
> - [x] Phase 1 — `workflow/service.py` bulk primitives (`bulk_cancel_reviews`,
>       `bulk_submit_decision`, `get_reviews`, batch-size validation),
>       no front door wired up yet
> - [x] Phase 2 — `api/routes.py`: `POST /reviews/bulk/cancel`,
>       `POST /reviews/bulk/decision`
> - [x] Phase 3 — `bff/ui.py`: in-process `_bulk_selection` store +
>       `POST /ui/{operator,manager}/bulk-select`; row/list templates
>       render `checked` from server state, no client-side selection JS
> - [x] Phase 4 — `bff/`: confirm dialog (lists selected items, reads
>       selection server-side) + execute routes + results display +
>       table refresh + selection clearing
> - [x] Phase 5 — tests + docs (`CLAUDE.md`)
>
> All 5 phases implemented and verified end to end against the real local
> stack (Keycloak in Docker, native Temporal/worker/Postgres) -- 86/86
> tests pass (`pytest`, unit + integration). One deviation from this
> file's original route sketch, made during implementation: the
> dialog-open routes (`bulk-cancel-form`, `bulk-decision-form`) ended up
> as `POST`, not `GET` like the existing single-item `-form` routes
> (`cancel-form`, `edit-form`) -- `bulk-decision-form` needs `decision` in
> its request, and both dialog-opens carry `page`/`query_id` forward so
> the post-action OOB table refresh lands back on the page the user was
> viewing (not spelled out in this file's original route sketch, but
> needed once "reflect the new statuses ... for the same page/query_id
> the user was already on" -- see "Results + table refresh" below -- was
> actually implemented, since the execute routes have no other way to
> know that page). One more implementation choice not spelled out below:
> `_operator_row.html`/`_manager_row.html`'s checkbox `<td>` renders
> (empty) even for a non-selectable row, whenever the column exists on
> the page at all (i.e. the user holds the relevant permission) --
> keeping the column's own header/body alignment intact matters more
> here than the literal "only render the checkbox `<td>` when
> `can_select` phrasing below.
>
> **Addendum (superseded naming, see `docs/MERGE_CANCEL_DECISION_PLAN.md`):**
> the separate bulk-cancel code path this file describes throughout
> (`bulk_cancel_reviews()` in `workflow/service.py`; `POST
> /reviews/bulk/cancel` in `api/routes.py`; `bulk-cancel-form`/
> `bulk-cancel` in `bff/ui.py`) has since been merged into
> `bulk_submit_decision()`/`POST /reviews/bulk/decision`/
> `bulk-decision-form`/`bulk-decision`, the same routes this file
> already describes for approve/reject, now also handling
> `decision="CANCELLED"`. The design below (selection store, dialog,
> execute, table refresh) is otherwise unchanged — only the cancel-
> specific function/route names it references are gone. Read
> `docs/MERGE_CANCEL_DECISION_PLAN.md` for the full rationale and the
> permission-architecture decision (drop role-based gating) that came
> with it.

## Decisions already made (don't re-litigate without new information)

- **One shared comment per batch**, not one per selected row. A single
  textarea in the confirm dialog; its value is sent once and applied as
  `closed_comment` to every request in the batch, via the same `comment`
  parameter `cancel_review()`/`submit_decision()` already take per call.
- **Best-effort execution, per-item results.** Each selected request is
  an independent Temporal workflow — there is no cross-workflow
  transaction to make this atomic in any real sense. A bulk action
  processes every selected id regardless of whether earlier ones failed,
  and returns a per-item `{request_id, ok, error}` list. The UI renders
  this as a results summary ("18 succeeded, 2 failed: ..."), not a
  single pass/fail.
- **Bulk cancel is Operator-only, bulk approve/reject is Manager-only**
  — same as the existing single-item actions. No new permission is
  introduced; bulk actions reuse the existing `Cancel`/`Approve`/`Reject`
  Keycloak scopes (see CLAUDE.md's `api/auth.py` bullet). A bulk call is
  authorized exactly like N individual calls would be — it doesn't
  bypass or loosen anything.
- **Mixed `review_type` in one batch is allowed.** Cancel/approve/reject
  are payload-agnostic (see CLAUDE.md's `workflows.py` bullet — the
  workflow never inspects `payload`), so there's no reason to force a
  single-type selection. The confirm dialog just lists each item's type
  alongside it.

## Design

### Why this can't be "one signal to N workflows"

Every review request is its own `ReviewApprovalWorkflow` execution,
addressed by its own workflow id (`review-{request_id}`). Temporal has
no concept of "signal these 20 workflow ids in one call" — a bulk action
is mechanically **N independent calls to the existing single-item
service functions**, run concurrently and collected. This is a thin
orchestration layer over `cancel_review()`/`submit_decision()`, not a
new code path with its own ownership/status logic — those functions
already do the real work (ownership check, `PENDING_REVIEW` check,
`_signal_or_reconcile()`, `_wait_until()` confirmation). Reusing them
means a bulk action inherits deleted-workflow recovery, the
already-decided guard, etc. for free, with no duplicated logic to drift
out of sync.

### `workflow/service.py` additions (Phase 1)

```python
_MAX_BULK_SIZE = 50  # generous for a POC; see "Operational considerations" below

@dataclass
class BulkActionResult:
    request_id: str
    ok: bool
    error: Optional[str] = None


def _validate_bulk_ids(request_ids: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(request_ids))  # de-dup, preserve order
    if not deduped:
        raise ValueError("request_ids must not be empty")
    if len(deduped) > _MAX_BULK_SIZE:
        raise ValueError(f"a single bulk action supports at most {_MAX_BULK_SIZE} requests")
    return deduped


async def get_reviews(pool: asyncpg.Pool, request_ids: list[str]) -> list[dict]:
    """Batch fetch, for rendering the bulk confirm dialog's preview list.
    Read-only, no ordering/visibility enforcement of its own -- callers
    that need the operator-visibility invariant (see CLAUDE.md) must
    filter the *ids they submit* to ones the session is actually allowed
    to see; this just resolves ids to rows."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM review_requests WHERE id = ANY($1::text[])", request_ids
        )
    return [dict(r) for r in rows]


async def bulk_cancel_reviews(
    client: Client, pool: asyncpg.Pool, request_ids: list[str], requester: str, comment: str = ""
) -> list[BulkActionResult]:
    ids = _validate_bulk_ids(request_ids)

    async def _one(request_id: str) -> BulkActionResult:
        try:
            await cancel_review(client, pool, request_id, requester, comment)
            return BulkActionResult(request_id, True)
        except (LookupError, PermissionError, ValueError) as e:
            return BulkActionResult(request_id, False, str(e))

    return await asyncio.gather(*(_one(rid) for rid in ids))


async def bulk_submit_decision(
    client: Client, pool: asyncpg.Pool, request_ids: list[str],
    decision: str, closed_by: str, comment: str = "",
) -> list[BulkActionResult]:
    ids = _validate_bulk_ids(request_ids)

    async def _one(request_id: str) -> BulkActionResult:
        try:
            await submit_decision(client, pool, request_id, decision, closed_by, comment)
            return BulkActionResult(request_id, True)
        except (LookupError, ValueError) as e:
            return BulkActionResult(request_id, False, str(e))

    return await asyncio.gather(*(_one(rid) for rid in ids))
```

Notes:
- `_one()` catches exactly the exception types the wrapped single-item
  function is documented to raise, same as both front doors already do
  at the route level. Anything else (a genuine bug, an unexpected
  exception type) is **not** caught here — it propagates out of
  `asyncio.gather()` and fails the whole request loudly, rather than
  being silently swallowed into a misleading per-item "failed" result.
- `submit_decision()`'s own `decision not in ("APPROVED", "REJECTED")`
  check raises `ValueError` before touching Postgres at all — validating
  `decision` once, before the loop, in `bulk_submit_decision()` (rather
  than letting the per-id wrapper catch it 50 times) is a reasonable
  small optimization but not load-bearing; either is correct.
- `asyncio.gather()` preserves input order in its result list, so
  `BulkActionResult`s line up positionally with `ids` — no separate
  correlation needed, though the dataclass carries `request_id` anyway
  since the UI needs it regardless of ordering.

### `api/routes.py` additions (Phase 2)

```
POST /reviews/bulk/cancel
  body: {"request_ids": [...], "comment": ""}
  -> require_permission("Cancel")
  -> service.bulk_cancel_reviews(client, pool, request_ids, user["sub"], comment)

POST /reviews/bulk/decision
  body: {"request_ids": [...], "decision": "APPROVED"|"REJECTED", "comment": ""}
  -> permission = "Approve" if decision == "APPROVED" else "Reject"; check_permission(user, permission)
  -> service.bulk_submit_decision(client, pool, request_ids, decision, user["sub"], comment)
```

Response shape (both routes):
```json
{
  "results": [{"request_id": "...", "ok": true, "error": null}, ...],
  "succeeded": 18,
  "failed": 2
}
```

`ValueError` from `_validate_bulk_ids()` (empty list, over the cap) maps
to `400`, same as every other `ValueError` this router already handles —
no new exception-to-status-code mapping needed.

### BFF: selection UI (Phase 3)

**Revised design (supersedes an earlier draft of this section that kept
selection in client-side JS)**: instead of tracking selection in the
browser and re-applying it to freshly-swapped DOM after every poll tick,
the server is the sole source of truth for "what's selected," and every
row render (self-poll, Prev/Next, or post-bulk-action refresh) bakes the
correct `checked` state directly into the HTML it sends. This sidesteps
the polling-wipe problem entirely — there is no client-side state to
lose on a swap, because nothing client-side is authoritative. It also
means the confirm-dialog and execute routes (Phase 4) don't need the
selected id list submitted to them at all; they just read it
server-side. Net effect: **no custom selection-tracking JS** — checkbox
interactions are plain `hx-post`s, same idiom as everything else in
`bff/templates/`.

**Storage** — `bff/ui.py` (not `workflow/service.py`: this is a BFF-UI
interaction convenience, not something the REST API needs — its bulk
endpoints take an explicit `request_ids` list in the body, see Phase 2):

```python
# username -> set of selected request_ids. In-process only, same
# "correct without a shared cache, a miss on another K8s replica just
# means the box renders unchecked -- never wrong data" reasoning as
# service.py's _query_cache (see docs/PAGINATION_PLAN.md). If/when
# docs/SESSION_STORE_PLAN.md's Redis session store lands, this can move
# there for free -- _get_selection()/_set_selection()/_clear_selection()
# stay the seam, only their bodies would change.
_bulk_selection: dict[str, set[str]] = {}

def _get_selection(username: str) -> set[str]:
    return _bulk_selection.setdefault(username, set())

def _clear_selection(username: str) -> None:
    _bulk_selection.pop(username, None)
```

Keyed by `username`, not a per-tab/per-login session id (which nothing
in this app currently mints for `/ui/*` sessions — see
`bff/keycloak_session.py`'s session shape). Two tabs open as the same
user share one selection set; accepted as a minor, low-probability edge
case, same tone as `docs/SESSION_STORE_PLAN.md`'s own "no
multi-session-per-user semantics" note.

**Routes**:
```
POST /ui/operator/bulk-select   (request_ids: list[str], checked: bool)
POST /ui/manager/bulk-select    (request_ids: list[str], checked: bool)
```
Gated by `require_session_role("operator"/"manager")` only — same as the
page itself, **not** `require_permission("Cancel")`/etc. Marking a row
"selected" has no side effect beyond what's rendered back to this same
user; it isn't a mutating action, so it doesn't need the heavier
permission check. The real enforcement point stays where it already is:
`cancel_review()`/`submit_decision()`, invoked from the Phase 4 execute
routes. Handler:
```python
@router.post("/operator/bulk-select", response_class=HTMLResponse)
async def bulk_select(
    request: Request,
    request_ids: list[str] = Form([]),
    checked: bool = Form(...),
    user: dict = Depends(require_session_role("operator")),
):
    sel = _get_selection(user["username"])
    (sel.update if checked else sel.difference_update)(request_ids)
    return HTMLResponse("")
```

**Wiring a checkbox to it** — a single row's checkbox:
```html
<input type="checkbox" name="request_ids" value="{{ r.id }}"
  {% if r.id in selected_ids %}checked{% endif %}
  hx-post="/ui/operator/bulk-select" hx-swap="none"
  hx-vals='js:{"request_ids": ["{{ r.id }}"], "checked": event.target.checked}'>
```
`hx-vals`'s `js:` prefix (htmx4 — see the `htmx4` skill) is required
here, not optional: native HTML form-encoding only includes a checkbox's
value when it's *checked*, so an unchecking click would otherwise send
no `request_ids` at all and the server could never learn "remove this
id." Explicitly reading `event.target.checked` sidesteps that. `hx-swap
= "none"`: the checkbox's own visual state is already correct the
instant the user clicks it (it's a real `<input>`); the POST is purely
to persist that fact server-side for the *next* render, nothing needs to
swap back.

A "select all on this page" checkbox in `<thead>` uses the same route,
with `request_ids` baked in at render time as the full list of this
page's selectable ids (`{{ selectable_ids | tojson }}` inside the `js:`
object) — same "bake this render's values in as literals, no
client-side dataset reads" idiom `_operator_list.html` already uses for
`page`/`query_id`.

**Rendering selection state and the toolbar** — `_operator_row.html`/
`_manager_row.html` macros gain a `selected_ids: set[str] = set()`
parameter (default empty, same pattern as their existing `permissions=[]`
default) and only render the checkbox `<td>` at all when
`can_select = r.status == "PENDING_REVIEW" and <relevant permission(s)>`
(operator: `"Cancel" in permissions`; manager: `"Approve" in permissions
or "Reject" in permissions`). `_operator_list.html`/`_manager_list.html`
fetch `_get_selection(user["username"])` in their route handlers
(`operator_list()`, `manager_list()`, and the initial `operator_page()`/
`manager_page()`) and pass it through as `selected_ids`, same as
`permissions` already is.

The "`N` selected" count and the bulk action button(s) (`Bulk Cancel` /
`Bulk Approve` / `Bulk Reject`) live **inside** `_operator_list.html`/
`_manager_list.html` — e.g. an extra `<tr>` in `<thead>` spanning all
columns — rather than in `operator.html`/`manager.html` outside the
table. This matters: because the toolbar is inside the same element the
5-second poll's `outerHTML` swap replaces, it's guaranteed to reflect
the current selection count on every tick, with zero extra plumbing (no
OOB swap, no separate count endpoint) — it's computed server-side from
`len(selected_ids)` at render time, same as everything else in that
template.

**Clearing selection**:
- `operator_page()`/`manager_page()` (the full-page `GET` routes, i.e. a
  fresh navigation/reload) call `_clear_selection(user["username"])`
  before rendering — satisfies "clear on a fresh reload."
- `operator_list()`/`manager_list()` (the `POST .../list` poll/Prev/Next
  route) must **not** clear it — it only reads current selection to
  render checked state.
- The Phase 4 execute routes (`bulk-cancel`, `bulk-decision`) clear it
  after processing — satisfies "clear after a confirmed action."

### BFF: confirm dialog + execute (Phase 4)

New routes, mirroring the existing dialog-then-execute pattern
(`.../cancel-form` → `.../cancel`, `.../edit-form` → `.../update`) —
**neither the dialog-open nor the execute route takes a request_ids
body param.** Because Phase 3 made the server the source of truth for
selection, both routes just read `_get_selection(user["username"])`
themselves. This also removes the earlier concern about a
client-submitted id list needing re-validation — there's no such list in
the request at all to validate.

```
Operator:
  POST /ui/operator/bulk-cancel-form   (no body)
    -> require_permission("Cancel")
    -> ids = _get_selection(user["username"])
    -> service.get_reviews(pool, ids), filtered to requester == user["username"]
    -> renders _bulk_confirm_dialog.html (action="Cancel")

  POST /ui/operator/bulk-cancel        (comment)
    -> require_permission("Cancel")
    -> ids = _get_selection(user["username"])
    -> service.bulk_cancel_reviews(client, pool, list(ids), user["username"], comment)
    -> _clear_selection(user["username"])
    -> renders results + OOB table refresh

Manager:
  POST /ui/manager/bulk-decision-form  (decision)
    -> check_permission(user, "Approve" if decision == "APPROVED" else "Reject")
    -> ids = _get_selection(user["username"])
    -> service.get_reviews(pool, ids)
    -> renders _bulk_confirm_dialog.html (action=decision)

  POST /ui/manager/bulk-decision       (decision, comment)
    -> check_permission(user, ...)
    -> ids = _get_selection(user["username"])
    -> service.bulk_submit_decision(client, pool, list(ids), decision, user["username"], comment)
    -> _clear_selection(user["username"])
    -> renders results + OOB table refresh
```

`.../bulk-cancel-form` still filters `get_reviews()`'s results to rows
whose `requester == user["username"]` before rendering the preview —
not because the id list is client-submitted anymore (it isn't — it's
read from server state that only that user's own `bulk-select` POSTs
ever populate), but because it's the cheapest place to keep the
**visibility invariant** visibly true in the preview too, same principle
as every other operator route (see CLAUDE.md's "Visibility" invariant
bullet). In practice `_bulk_selection[username]` should never contain
another user's id in the first place, since `bulk_select()` only ever
mutates the caller's own entry — this filter is defense in depth, not
the only thing standing between an operator and someone else's request.
Manager routes need no such filter (managers see everything).

An id can still legitimately go stale between being selected and the
dialog opening (someone else acted on it, or it's now off the page the
user last looked at) — that's fine, exactly the "best-effort, per-item
results" semantics decided earlier; the preview just shows whatever
`get_reviews()` currently returns for those ids, and the execute call
re-validates status/ownership the same way the single-item routes always
have.

`_bulk_confirm_dialog.html` (one shared template for all three actions,
following the same "one macro/template, multiple call sites" pattern
already established by `_operator_row.html`/`_manager_row.html` rather
than the badge-color-dict copy-paste pattern):
- Takes `action` ("Cancel"/"Approve"/"Reject"), `items` (the fetched
  records), `post_url`, `role` (whether to show a Requester column).
- Lists each item: type, (requester, manager only), current status.
- One shared comment `<textarea>`.
- Confirm button: same "read a value, close the dialog client-side, fire
  `htmx.ajax()`" pattern as every other dialog's confirm button
  (`bff/ui.py`'s docstring / CLAUDE.md's "Every mutating action" bullet)
  — but simpler than those, since there's no id list or `source` row to
  wire up (a bulk action doesn't map to one row's stable-id button the
  way Edit/Cancel/Approve/Reject do). It only needs to read the comment
  textarea and POST it (plus `decision`, for the manager case) — the
  server already knows which ids it's acting on.
- Target for the execute response: `#dialog-container` (a results
  fragment, not a single row).

**Results + table refresh**: the execute routes' response needs to (a)
show per-item pass/fail and (b) reflect the new statuses in the table,
including for any selected rows that aren't on the currently-displayed
page. Two things in one response, via the OOB pattern already used for
`clear_dialog` in `_operator_list.html`:
- Primary content (swapped into `#dialog-container`): a small
  `_bulk_result_dialog.html` — "18 succeeded, 2 failed", with failures
  listed by id + error message, and a Close button.
- An out-of-band fragment appended after it: `_operator_list.html`/
  `_manager_list.html` re-rendered for the *same page/query_id the user
  was already on*, with `hx-swap-oob="true"` added to the `<table
  id="request-list">` tag (parameterize these templates with an `oob:
  bool` flag the same way `clear_dialog` already parameterizes the
  dialog-clear behavior).

Since the execute route already called `_clear_selection()` before
rendering this response, the OOB-refreshed table's own `selected_ids` is
empty — every checkbox renders unchecked and the toolbar's "N selected"
correctly reads 0, with no client-side reset step needed.

### Operational considerations

- `_MAX_BULK_SIZE = 50` caps how many concurrent `cancel_review()`/
  `submit_decision()` calls (each of which does its own signal +
  `_wait_until()` poll loop, i.e. its own Postgres connection + Temporal
  RPC) one request can trigger. This interacts with the pool-sizing note
  already in CLAUDE.md's Kubernetes section (`asyncpg` pool `max_size` ×
  replica count) — a bulk request briefly holds up to 50 connections
  from the pool at once. 50 is a reasonable POC default; revisit
  alongside real pool sizing before treating it as load-bearing at
  larger scale.
- No new Postgres schema/columns — every row in a batch is still just an
  independent `cancel_review()`/`submit_decision()` call writing the
  same `status`/`closed_by`/`closed_comment`/`closed_at` columns that
  already exist.

## Phase 5 — tests + docs

**Tests** (`tests/integration/`, needs the real stack up, same
`@pytest.mark.integration` convention as the rest of the suite):
- Bulk cancel: create N pending requests as `operator1`, bulk-cancel all
  → every row `CANCELLED`, `closed_by="operator1"`, shared comment on
  all of them.
- Bulk cancel with a mix of eligible and already-terminal ids → per-item
  results correctly separate the two; eligible ones still get processed.
- Bulk decision (approve and reject) as `manager1` → correct statuses;
  `Approve`-only or `Reject`-only permission (if ever split further)
  would show up as a per-item permission failure, not a whole-batch 403
  — worth a test once/if that becomes a real scenario, low priority now
  since both are currently granted together to `Manager Policy`.
- REST API: bulk endpoints reject with `403` for a caller without the
  relevant permission (no per-item execution at all — the route-level
  `require_permission`/`check_permission` gate runs before the loop).
- REST API: empty `request_ids` and over-the-cap `request_ids` both `400`.
- BFF: `bulk-select` only ever mutates the calling session's own entry in
  `_bulk_selection` — two logged-in users (`operator1`, `operator2`)
  selecting concurrently never see or clear each other's selections.
- BFF: selecting rows, then hitting the confirm-dialog route, shows
  exactly those rows in the preview; confirming clears the selection
  (a subsequent `GET /ui/operator` or `/ui/manager` page render shows no
  checked boxes); a fresh `GET` (not the poll route) also clears it even
  without confirming anything.
- BFF (operator only): if `_bulk_selection` somehow held another
  operator's request id (shouldn't happen given the above, but worth
  covering as defense-in-depth), the confirm dialog's preview silently
  drops it rather than displaying it (visibility invariant).

**Docs** (`CLAUDE.md`):
- New bullets under `workflow/service.py`, `api/routes.py`, `bff/ui.py`,
  and `bff/templates/` describing the bulk primitives/routes/templates,
  matching the density of the existing bullets for those files.
- A line under "Invariants" if any of the existing invariant bullets
  (visibility, terminal-states-are-view-only, ownership) need an
  explicit "...and bulk actions enforce this the same way, per item" note
  rather than leaving it implicit.

## Deliberately out of scope for this plan

- **Per-row comments within a bulk action.** Rejected in favor of one
  shared comment — see "Decisions already made".
- **Atomic/all-or-nothing bulk semantics.** Not meaningful across
  independent Temporal workflow executions without a coordinating
  workflow of its own (e.g. a saga), which is a much larger change for
  a POC-stage feature; best-effort + per-item results was the explicit
  choice here.
- **A "select all matching this filter" (beyond the current page)
  option** — e.g. "select all 340 pending requests across every page."
  Only page-by-page manual selection (plus per-page "select all on this
  page") is in scope; a filter-wide select-all would need either
  fetching every matching id upfront (defeats the purpose of pagination)
  or a server-side bulk-by-filter endpoint (a different, larger design).
  Revisit only if manual multi-page selection turns out to be too
  tedious in practice.
- **Bulk edit/update payloads.** Only the three terminal-transition
  actions (cancel/approve/reject) are in scope, matching the user-facing
  ask; `update_payload` isn't a bulk-shaped operation in the same way
  (each row's payload edit is independently meaningful content, not a
  shared value like a comment).
