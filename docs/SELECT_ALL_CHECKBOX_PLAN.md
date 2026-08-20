# Select-all checkbox redesign

> **Status tracker** — update this block as each phase's steps land, so
> a new session can tell exactly where to resume just by reading this
> file (same convention as `docs/PAGINATION_PLAN.md`,
> `docs/SESSION_STORE_PLAN.md`, `docs/BULK_ACTIONS_PLAN.md`).
>
> - [x] Phase 1 — split `_operator_list.html`/`_manager_list.html` into
>       three independently-swappable fragments (header row, toolbar,
>       tbody); move the 5s poll's `hx-trigger` off the whole `<table>`
>       and onto just the tbody
> - [x] Phase 2 — `bff/ui.py`: new/adjusted route(s) so the periodic poll
>       returns only a tbody fragment, while a real page load/Prev/Next
>       still render the full three-fragment table
> - [x] Phase 3 — select-all checkbox: declarative `hx-post`/`hx-vals`
>       (no custom JS) targeting the tbody fragment; drop the
>       `all_selected` computation entirely (see "Decisions" below)
> - [x] Phase 4 — toolbar fragment gets a stable id; every bulk-select
>       response (row-level and select-all) carries an OOB swap for it;
>       verified against the real htmx4 bundle (downloaded and
>       disassembled, not just read about) that OOB fragments are built
>       and dispatched as independent swap tasks with their own
>       `swapSpec`, regardless of the triggering element's own primary
>       `hx-swap` value — see "Decisions" below for what was actually
>       found in the bundle
> - [x] Phase 5 — tests + docs
>
> Implemented and verified server-side end to end against the real local
> stack (92/92 tests pass, `pytest`) -- new templates
> (`_operator_bulk_toolbar.html`, `_manager_bulk_toolbar.html`,
> `_operator_rows.html`/`_operator_rows_body.html`,
> `_manager_rows.html`/`_manager_rows_body.html`), new routes
> (`POST /ui/{operator,manager}/rows` for the periodic poll), and
> `bff/ui.py`'s `_toolbar_oob()` helper wired into every route that
> could otherwise leave the toolbar fragment stale (Prev/Next,
> `create_request()`, both `bulk-select` routes, both bulk-execute
> routes via `_bulk_result_response()`).
>
> **Phase 6 (unplanned) — the actual root cause, found after user
> testing in a real browser: `request_ids` was a JS array in every
> `hx-vals`, and htmx4 silently mangles array values.** The three-region
> redesign above (Phases 1-5) was real, necessary architecture (the
> table genuinely needed to stop self-polling to make room for a stable
> select-all checkbox), but it did **not** actually fix select-all's
> broken *value* — the true bug, present since this feature's very
> first version, was that `hx-vals`'s array value for `request_ids` gets
> passed straight to `FormData.set(name, value)`, which (per spec)
> stringifies a non-string/non-Blob value via `Array.prototype.
> toString()` — comma-joining it into **one** field, not repeating the
> field once per element. `bff/ui.py`'s routes were parsing
> `list[str] = Form([])`, expecting repeated fields — so a multi-id
> `request_ids` array (only ever sent by select-all) silently became one
> garbage comma-joined "id" that never matched anything, while a
> per-row checkbox's single-element array happened to produce the
> correct value by accident (nothing to comma-join). This exactly
> explains the symptoms reported after Phases 1-5 shipped: the selected
> *count* changed (something real was being added to the selection set,
> just the wrong thing), the bulk-action button's enabled state followed
> that same wrong count, and no row checkbox ever actually became
> checked. **Fix**: templates now build `request_ids` explicitly as a
> string (`.join(",")` for select-all, a bare id for a per-row checkbox)
> instead of an array; `bff/ui.py`'s two `bulk-select` routes now take
> `request_ids: str = Form("")` and split on `,` (ids are UUIDs, never
> containing a literal comma). Confirmed against the real pinned bundle
> (`htmx.org@4.0.0-beta6`, disassembled): its `hx-vals` handling is
> `n.set(e, t[e])`, never `n.append()`, with no array-aware
> special-casing anywhere in that path.
>
> **Lesson for next time this kind of bug shows up**: when a
> multi-select-style declarative htmx interaction "doesn't work" but a
> single-select version of the same pattern does, suspect the *wire
> format* (does the array survive request serialization intact?) before
> suspecting DOM structure/swap targeting — this cost two prior fix
> attempts and a full architectural redesign before the real cause
> surfaced, entirely because concrete user-reported browser symptoms
> (not more server-side reasoning) were what finally pointed at it.
> **Still not independently confirmed by a direct visual click-test** —
> the Chrome extension remained unavailable for this entire
> investigation; the fix follows directly from the real bundle's own
> source and from the user's reported symptoms matching it exactly, but
> hasn't been eyeballed working end to end in an actual browser.

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
  enabled state are correct immediately after *any* checkbox interaction.
  **Confirmed against the real pinned bundle** (`htmx.org@4.0.0-beta6`,
  downloaded and disassembled, not just read about): OOB elements are
  collected by a dedicated handler that builds them as independent
  `{type: "oob", target, swapSpec, ...}` tasks — `swapSpec` parsed from
  each element's own `hx-swap-oob` attribute value (defaulting to
  `outerHTML` for `hx-swap-oob="true"`), never inherited from whatever
  the *main* task's swap style is. The `if (style === "none") return;`
  early-out that makes `hx-swap="none"` a no-op only short-circuits the
  one task whose own `swapSpec.style` is `"none"` — an OOB task's style
  comes from its own attribute, so a per-row checkbox's `hx-swap="none"`
  on its *main* target has no effect on a same-response OOB fragment's
  own swap.

## How it was actually built (resolving the open questions below)

- **`bulk-select` stayed one shared route** for both per-row and
  select-all clicks, exactly as originally guessed. It always renders
  and returns two fragments: `_{role}_rows.html` (the current page's
  rows, `<table>`-wrapped) — meaningfully used as the primary swap
  target only by the select-all checkbox's own request
  (`hx-target="#request-rows" hx-select="tbody" hx-swap="outerHTML"`),
  ignored by a per-row checkbox's `hx-swap="none"` — plus
  `_{role}_bulk_toolbar.html` rendered with `oob=True`, always used.
  Both take `page`/`query_id` as `Form` params (default `0`/`""`), now
  sent by *every* checkbox (per-row included — see below), not just
  select-all.
- **Tag-stripping avoided the same way the single-row-response templates
  already do it**: `_operator_rows_body.html`/`_manager_rows_body.html`
  hold the actual `<tbody id="request-rows">...</tbody>` (with its own
  polling `hx-*` attributes baked in), included both directly inside the
  live `<table>` in `_operator_list.html`/`_manager_list.html` (full
  render) and wrapped in a bare `<table>...</table>` by
  `_operator_rows.html`/`_manager_rows.html` (the standalone poll/
  bulk-select response). `hx-select="tbody"` on every request targeting
  `#request-rows` directly (the self-poll, and the select-all checkbox)
  pulls the inner `<tbody>` back out before the swap.
- **Select-all is additive/replace-exact, not merge-toggle**: checking it
  sends *all* of the page's selectable ids with `checked: true`
  (`sel.update(request_ids)` — a set union with whatever was already
  selected, including rows on other pages); unchecking it sends the same
  ids with `checked: false` (`sel.difference_update` — removes exactly
  those ids, leaving other pages' selections alone). It does not first
  clear the whole selection.
- **Toolbar stable ids**: `#bulk-toolbar-operator` / `#bulk-toolbar-manager`.
  **Tbody stable id**: `#request-rows`, shared by both roles (never both
  present in the DOM at once, since a session is either the operator or
  manager screen). The toolbar got its own small dedicated template per
  role (`_operator_bulk_toolbar.html`/`_manager_bulk_toolbar.html`) —
  plain templates, not `{% macro %}`s, since each is only ever rendered
  as a whole fragment (inline or OOB), never called multiple times
  per-row the way `_operator_row.html`'s macro is.
- **Every route whose response could leave the toolbar's baked-in
  `page`/`query_id` or selected-count stale now explicitly refreshes it**
  via `bff/ui.py`'s `_toolbar_oob()` helper: Prev/Next
  (`operator_list()`/`manager_list()`), `create_request()` (view resets
  to page 0), both `bulk-select` routes, and both bulk-execute routes
  (via `_bulk_result_response()`, which now takes a `role: str` instead
  of a literal template name so it can render the matching toolbar too).
  The one route that does **not** touch the toolbar is the periodic poll
  (`operator_rows()`/`manager_rows()`) — polling alone never changes
  selection, so there's nothing for the toolbar to reflect.
