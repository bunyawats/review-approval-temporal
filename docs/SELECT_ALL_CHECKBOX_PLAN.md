# Select-all checkbox redesign

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (same convention as `docs/PAGINATION_PLAN.md`,
> `docs/SESSION_STORE_PLAN.md`, `docs/BULK_ACTIONS_PLAN.md`).
>
> - [ ] Phase 1 — split `_operator_list.html`/`_manager_list.html` into
>       three independently-swappable fragments (header row, toolbar,
>       tbody); move the 5s poll's `hx-trigger` off the whole `<table>`
>       and onto just the tbody
> - [ ] Phase 2 — `bff/ui.py`: new/adjusted route(s) so the periodic poll
>       returns only a tbody fragment, while a real page load/Prev/Next
>       still render the full three-fragment table
> - [ ] Phase 3 — select-all checkbox: declarative `hx-post`/`hx-vals`
>       (no custom JS) targeting the tbody fragment; drop the
>       `all_selected` computation entirely (see "Decisions" below)
> - [ ] Phase 4 — toolbar fragment gets a stable id; every bulk-select
>       response (row-level and select-all) carries an OOB swap for it;
>       verify against the real htmx4 bundle that an OOB fragment is
>       still processed on a response whose *primary* target uses
>       `hx-swap="none"` before relying on it
> - [ ] Phase 5 — tests + docs
>
> Not started. This document is the output of a design brainstorm run
> entirely in conversation (no code changes) after two earlier, reverted
> attempts to fix the select-all checkbox bug — see "History" below.
> Read this whole file before touching `_operator_list.html`/
> `_manager_list.html`/`_operator_row.html`/`_manager_row.html`/the
> `bulk-select` routes in `bff/ui.py` again.

## History (don't repeat these)

The select-all checkbox, as originally built in `docs/BULK_ACTIONS_PLAN.md`
Phase 3, never worked in a real browser: clicking it changed neither its
own visible state nor any row's. Two fix attempts were tried and reverted
in the same session that first shipped it:

1. **`hx-vals='js:{...event.target.checked...}'` + `hx-target="#request-list"
   hx-swap="outerHTML"`** — same declarative pattern as the per-row
   checkboxes, but swapping the whole table (an *ancestor of the checkbox
   itself*) via `outerHTML`. Verified correct server-side (direct HTTP
   requests against the running app showed the right HTML coming back),
   but didn't fire correctly from a real browser click.
2. **`onclick="...htmx.ajax(...)"`** — the same escape-hatch pattern this
   app's dialogs already use for computed values (see
   `_cancel_dialog.html`). Same self-swap-of-ancestor structure as
   attempt 1, just issuing the request imperatively instead of via
   declarative attributes. Also didn't work.

Neither attempt's root cause was confirmed against real browser devtools
(console/Network tab) — the Chrome extension wasn't connected in that
session, so diagnosis stayed server-side-only, which wasn't where the bug
was. **The redesign below sidesteps the question entirely** rather than
resolving it: it restructures the table so the select-all checkbox's
target is never its own ancestor (it targets a *sibling* `<tbody>`
instead), which was the one structural trait both failed attempts shared
and nothing else in this app's already-working htmx code exercises (see
`docs/BULK_ACTIONS_PLAN.md`'s "Prev/Next" buttons for the one existing
counter-example — those also self-swap an ancestor successfully, but
they're `<button>`s issuing plain literal `hx-vals`, not a checkbox
combining a native pre-toggled `checked` property with a `js:`-computed
value in the same request).

## Decisions already made (don't re-litigate without new information)

- **Keep client-side JavaScript to a minimum.** Prefer declarative htmx
  attributes (`hx-post`, `hx-target`, `hx-swap`, `hx-vals`) over hand-written
  `onclick`/`htmx.ajax()`. This is why the redesign restructures the DOM
  (so the declarative pattern becomes safe to use again) rather than
  keeping the `onclick` escape hatch from failed attempt 2.
- **Three independently-refreshable regions, not one monolithic table
  swap.** Splitting `<thead>`'s column-header row, a separate toolbar
  fragment, and `<tbody>` apart is the central structural change:

  | Region | Refreshed by |
  |---|---|
  | **Header row** (column labels + select-all checkbox) | Real page navigation only: initial `GET /ui/{operator,manager}`, Prev/Next, post-bulk-action refresh. **Never** by the 5s poll. |
  | **Toolbar fragment** ("N selected" + bulk action button(s)) | Any checkbox handler firing — a per-row check/uncheck, or the select-all checkbox — via an out-of-band swap carried in that response. Independent of both the poll and the header row's own (much rarer) refresh. |
  | **Table body (rows)** | The 5s poll, Prev/Next, initial load, and the select-all checkbox's own action. **Not** an individual per-row checkbox click, which stays self-correcting (`hx-swap="none"`, unchanged from the original Phase 3 design). |

- **The select-all checkbox is a stateless action trigger, not a status
  indicator.** It does **not** reflect "are all rows on this page
  currently selected" — there is no `all_selected` / `selectable_ids ∩
  selected_ids` computation. It always renders unchecked on every render
  of the header row (initial load, Prev/Next, post-bulk-action refresh),
  with no exception: not because that's computed to be correct for a
  given page, but because it never carries state across a render at all.
  It only visually shows checked in the moment between a user's click and
  the next header-row re-render (a native, uncomputed browser behavior).
- **Prev/Next re-renders the header row** (along with the toolbar and
  tbody, as a full page-navigation render) — this is *why* the select-all
  checkbox resets to unchecked on Prev/Next: not a special case, just a
  consequence of the header row being freshly rendered (always
  unchecked) for the new page.
- **Select-all is a real controller of every data-row checkbox, driven
  through the server, not a client-side-only toggle.** Checking it issues
  a declarative htmx request to the existing `/ui/{operator,manager}/
  bulk-select` route with *all* of the current page's selectable record
  ids and `checked: true`; unchecking it issues the same route with
  `checked: false`. The server-side `_bulk_selection` store (see
  `docs/BULK_ACTIONS_PLAN.md`) is the actual source of truth for every
  row's checked state, exactly as it already is for individual row
  checkboxes — select-all doesn't introduce a second, client-only
  selection mechanism.
- **The tbody refreshes immediately as part of that same request/response
  cycle** — not by waiting for the next 5s poll tick. This is the whole
  point of targeting the tbody directly from the select-all checkbox's
  own htmx action, rather than relying on the periodic poll to eventually
  pick up the new selection state.
- **The toolbar's OOB update rides along in every bulk-select response**,
  select-all or per-row, so "N selected" and the bulk-action button's
  enabled state are correct immediately after *any* checkbox interaction
  — this needs verifying against the real htmx4 bundle (per Phase 4
  above) that an out-of-band fragment is still processed when the
  triggering element's own primary `hx-swap` is `"none"` (the per-row
  checkbox case); expected to work since OOB processing is documented as
  an independent step from the primary swap, but not yet confirmed here.

## Open questions / not yet decided

- Exact route shape for `bulk-select`: does it stay one shared route for
  both per-row and select-all clicks (matching the original Phase 3
  "one route serves both cases without branching on which checkbox
  triggered it" philosophy), returning a tbody fragment (used as the
  primary swap target only by select-all, ignored by per-row's
  `hx-swap="none"`) plus an always-present toolbar OOB fragment? This
  seems like the natural continuation of the existing design but hasn't
  been explicitly confirmed.
- Exact mechanics for returning *only* a tbody fragment from the poll
  route without hitting the `<tr>`/`<td>`-outside-table-context tag
  stripping gotcha already documented in the `htmx4` skill and in
  `CLAUDE.md`'s `_operator_row.html`/`_manager_row.html` bullet (the
  existing single-row-response templates already solve this with a
  `<table><tbody>...</tbody></table>` wrapper + `select: 'tbody'`/`'tr'`
  — the poll's full-tbody response likely needs the same treatment).
- Whether clicking select-all while some (but not all) rows are already
  individually checked is additive (union with existing selection) or a
  full reset — not yet discussed.
- What a toolbar stable id / tbody stable id should be named, and whether
  the toolbar fragment needs its own `{% macro %}` like
  `_operator_row.html`/`_manager_row.html` already do for rows.
