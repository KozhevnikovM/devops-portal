## Context

See proposal.md (Why). Current shape:

**Row partials**
- `partials/booking_row.html` treats `READY`/`FAILED`/`RELEASED` as terminal. Only non-terminal rows carry `sse-swap="booking-<id>"` and `hx-get="/bookings/<id>/row" hx-trigger="every 60s" hx-swap="outerHTML"`.
- `partials/environment_row.html` does the same with `derived_status`.

**List sections and SSE**
- The list sections (`partials/booking_list_section.html`, `partials/environment_list_section.html`, #494) own the `<tbody … hx-ext="sse" sse-connect="/events/stream">`. A filter change replaces the whole section (`outerHTML`).
- Load more appends `partials/*_rows_page.html` in place of the load-more `<tr>`.
- The order forms prepend into the tbody with `afterbegin`.

**Single-row and SSE authorization**
- `GET /bookings/{id}/row` and `GET /environments/{id}/row` load the full entity and enforce `can_manage`, otherwise 403.
- The SSE stream (`events.py`) renders the same partial after `can_manage`.
- Neither path serves other users' rows on the All list. Those rows poll and get 403 forever.

**Repositories**
- `BookingRepository.list_page` phase 2 hydrates `_list_item_stmt().where(id.in_(ids))`, a list-safe `BookingListItem` projection.
- `EnvironmentRepository.list_page` phase 2 hydrates environments by `id.in_` plus `_children_batch(ids)`. Children are full `Booking` entities, which the template does not render beyond list fields.
- Neither hydration is exposed on a port.
- `attach_queue_positions` (`routes/_queue.py`, #495) ranks any set of rows in one statement.

**htmx 1.9.12 behaviour**, verified in `node_modules/htmx.org/dist/htmx.js`:
- Out-of-band swaps are processed even with `hx-swap="none"` (`selectAndSwap` → `handleOutOfBandSwaps`).
- `hx-swap-oob="delete"` is a supported swap style.
- A response whose first tag is `<tr>` is parsed inside `<table><tbody>`.
- `htmx:oobBeforeSwap` is cancellable.
- A response **is still processed after its triggering element has left the DOM**, so stale-response protection must be explicit.
- `every Ns` polling stops once the element is removed.

## Goals / Non-Goals

**Goals:**
- One background request per list section per 60 s, with server-bounded work and statements independent of batch size.
- Re-authorization by list visibility, using the list's projection and render path, so a reconciled row is byte-identical to the same row in a fresh list (modulo `is_first_row`, see D7).
- Changed rows only. Unchanged rows are never swapped, so open menus and label edits are not clobbered.
- A stated, tested convergence bound.

**Non-Goals:**
- Changing the SSE transport, channels or authorization. SSE still pushes only to users who may manage the row.
- Triggering an extra reconciliation on SSE reconnect. It would break "at most one per interval", and the bound already covers the gap.
- Inserting newly matching rows, re-sorting, or re-establishing the empty state after every row is removed. A reload or the new-rows indicator does that.
- Changing `list_page`'s own children read (`_children_batch`, which loads full entities with no bound). Reconcile uses its own bounded, list-safe children read (D3a). The list page's read is left as it is, and bounding it is a separate follow-up.
- A write-side limit on blueprint size (D3a alternatives).
- Reacting to viewer-context changes (timezone setting, role change) without a reload.
- Removing `GET /bookings/{id}/row` / `GET /environments/{id}/row`. They are documented and tested, and nothing polls them any more.

## Decisions

### D1. One endpoint per list page, GET, `hx-swap="none"` + out-of-band rows

`GET /book/vm/reconcile`, `GET /book/namespace/reconcile` and `GET /environments/reconcile` sit beside `/list` and `/rows` (`include_in_schema=False`, `require_user`). The parameters are:
- `filter`, `show_released`, `label`: the canonical list filters (`pagination.filter_params`);
- `r`, repeated, one per row: `"<uuid>.<version>"`;
- `newest`: optional, an `encode_cursor` key.

The longest request is about 50 × 54 bytes, roughly 2.7 KB, well within default proxy request-line limits (nginx 8 KB).

The response is a sequence of `<tr>` elements, each with `hx-swap-oob`:
- `<tr id="booking-<id>" hx-swap-oob="true" …>` for each changed row, which is a full row rendering;
- `<tr id="booking-<id>" hx-swap-oob="delete"></tr>` for each removal;
- one `<tr id="bookings-new-rows" hx-swap-oob="true">`, empty or holding the indicator button.

The response always starts with a `<tr>` (the indicator row is always present), so htmx's table-context parsing applies.

Why GET: the request is a read, it passes the CSRF-origin middleware without special-casing, and its access-log line can be suppressed like `/row` (`_SuppressRowPolling` gains the reconcile paths).

*Alternatives considered:*
- **POST with a JSON body.** No size advantage at these limits, and it would need CSRF handling for a read.
- **One shared `/reconcile` for all pages, with a `kind` parameter.** The page path already encodes the resource types and keeps the `<page_path>/<suffix>` convention.
- **Replacing the tbody with a re-rendered loaded window.** That costs server work proportional to all loaded pages and resets in-row UI state, both of which the issue rules out.

### D2. Bounds validated before any read

A small presentation helper (`app/presentation/reconcile.py`) parses `r`:
- A count above `settings.RECONCILE_MAX_IDS` → 400.
- A malformed uuid → 400.
- A duplicate uuid → 400.
- A version that is not `[0-9a-f]{16}` → 400.
- A malformed `newest` → 400, via strict `decode_cursor` like the Load more cursor.

All of these run before a session is used. Rejecting rather than truncating means a buggy client fails loudly in tests instead of silently under-reconciling.

`RECONCILE_MAX_IDS` defaults to 50 and is validated in `app/config.py` as `1 ≤ value ≤ min(BOOKINGS_PAGE_SIZE, ENVIRONMENTS_PAGE_SIZE)`. One reconcile therefore never reads more than a list page. `RECONCILE_SETTLED_MIN` (R) defaults to 10 and is validated as `1 ≤ value < RECONCILE_MAX_IDS`. The reserve is `min(S, R)`. Zero is rejected because, with `R = 0` and at least `B` rows in flight, settled rows would never be sent and the settled bound's denominator would be 0 (PR #512 review). Both values are rendered onto the poller element (D6), so the client uses the server's numbers, and the server still enforces the cap itself.

`RECONCILE_MAX_CHILDREN_PER_ENVIRONMENT` (M) defaults to 20 and is validated as `≥ 1`. It is the hard per-environment child bound of D3a.

### D3. Reuse list hydration as a port-level batch read; authorize in the query

**Bookings.** `BookingRepository.list_items_by_ids(session, ids, *, user_id, resource_types) -> list[BookingListItem]`:
- It is `list_page`'s phase-2 statement, extracted and shared by both callers, with the scope predicate added:
  - `resource_type IN :types` always;
  - `_owner_filter(user_id)` when `user_id` is given, which is Mine.
- `list_page` calls the same helper without the scope predicate, because its keys are already scoped.

**Environments.** `EnvironmentRepository.list_items_by_ids(session, ids, *, user_id)` does the same:
- environments by `id.in_` and `_list_stmt`'s Mine-OR rule;
- then a **bounded** children read (D3a), not `_children_batch`.

Both methods are declared on `BookingRepositoryPort` / `EnvironmentRepositoryPort` so tests can fake them.

**Authorization is the query.** An id is visible iff it comes back. The route computes `removed = requested − returned` and emits the same `delete` directive for missing and invisible ids (spec: forged ids are indistinguishable). Under All, `user_id=None` matches the All list's own `list_page(user_id=None)`. Every authenticated user already sees those rows' list projection, so reconciliation reveals nothing new.

Show released is **not** applied to the batch read. A displayed row that became RELEASED must be returned so it can render in its final state (spec). The label filter is not applied either.

**Queue ranks.** Bookings call `attach_queue_positions(session, repo, items)` once. It runs one statement, or none when no item is QUEUED.

**Statement budget.** These are exact maxima, counted as every statement the session sends, the way `before_cursor_execute` sees them. They include the `_OrderedWalk` plan pin and restore (one statement each) around each ordered index walk:

| Page | Batch read | Children (D3a) | Rank (pin + read + restore) | Newest probe (pin + keys read + restore, D8) | Max total |
|---|---|---|---|---|---|
| Bookings | 1 | — | 0 or 3 | 3 | **7** |
| Environments | 1 | 1 | — | 3 | **5** |

This holds whatever the number of ids. No catalog repo is touched. The integration test (task 7.1) asserts both the equality between 1 and `RECONCILE_MAX_IDS` ids and these maxima. If the plan-pin mechanism changes, the numbers move with it: they are pinned by that test, not by the spec's wording alone.

### D3a. Hard child bound for environments

An environment's child count is not bounded today. It equals its blueprint's item count, and blueprints have no item limit. So "no more than the list page would load" is not a server-enforced bound (PR #512 review).

The reconcile children read is one statement that takes at most `M + 1` children per requested environment:

```sql
SELECT c.<list-safe child columns>, …display joins…   -- never password/ssh key/log/script/vars
FROM unnest(:env_ids) AS e(id)
CROSS JOIN LATERAL (
    SELECT … FROM bookings b WHERE b.environment_id = e.id LIMIT :m_plus_1
) c
```

The lateral has no `ORDER BY`. A `LIMIT` over the existing `ix_bookings_environment_id` index then stops after `M + 1` index entries, with no sort over all of the children. When `M` or fewer come back, they are all of the children, and they are ordered by `created_at` in Python, the row's existing order. When `M + 1` come back, the environment is **oversized**, and the order does not matter, because it is not rendered. Children examined are at most `B × (M + 1)`. Rendered rows are at most `B`, each with at most `M` children.

**An oversized id** gets an OOB swap of the row's sync placeholder `<span id="environment-<id>-sync">`, which every non-final environment row renders (empty). The swap fills it with the hint "Not refreshed automatically — reload to update" and the attribute `data-reconcile-skip`. The client excludes rows containing `[data-reconcile-skip]` from both classes (D7). The row keeps `sse-swap`, so managed rows still get live pushes. The full-entity SSE/`/row` paths are unchanged.

**The list render.** It already loads every child, and it renders the same hint and marker up front when `len(children) > M`. Reconcile meets an oversized environment only if the configured bound was lowered after the page rendered. Environments are created with all their children at order time.

*Alternatives considered:*
- **A write-side blueprint item limit.** Existing data could already exceed it, so the read still needs its own bound. It is a policy change for admins, out of this change's scope.
- **Truncating children in every environment row.** That would change the list UI and require the derived status to come from an aggregate over all children, which is unbounded again.

*Alternatives considered:*
- **`can_manage` per row**, as `/row` does. That would keep the All list's foreign rows stale forever, the current bug, and would need a full `get` per row.
- **Re-checking visibility in Python after a broad read.** It reads rows the user may not see, for no gain.

### D4. Row version = truncated SHA-256 over list-safe values

`row_version(item) -> str` returns 16 hex characters (64 bits) of SHA-256 over a canonical serialization.

**Bookings.** The input is every field of `BookingListItem`, via `dataclasses.astuple`, which includes `queue_position`. The projection *is* the set of values the row can display. Hashing all of it means a newly displayed field cannot be forgotten. A field that is projected but not displayed only causes a harmless re-render.

**Environments.** The input is:
- the environment list fields (name, blueprint name, owner and creator names, `expires_at`, `derived_status`);
- for each child, in the row's order, `(id, status, environment_label, resource_type, namespace_name, static_vm_name, static_vm_host, image_name, vm_ip, config_failed)`.

These are the fields `environment_row.html` reads. The child tuple is explicit because children are full entities (non-goal above). A table-driven test asserts that changing each of those child fields changes the version, and that changing a child's password or provisioning log does not.

**Where the version is used.** Every row render path computes the version when building the template context, so the list, `/rows`, `/list`, `/row`, the SSE renderer and action responses all emit `data-row-version`.

**Bookings off the projection.** Paths that render from a full `Booking` (`/row`, SSE, the create response) build the same projection first:
- either through `list_items_by_ids([id])` with no scope;
- or through a `BookingListItem.from_booking` adapter.

The implementation picks one; the parity test in tasks pins the result. The version then cannot differ between paths for the same state. The viewer's identity is not an input, because a page has one viewer.

*Alternatives considered:*
- **A hash of the rendered HTML.** The first row renders differently (`is_first_row` menu placement), and a row cannot embed the hash of its own markup without a second render.
- **`updated_at`.** Bookings have no reliable row-level `updated_at`, and child and queue-rank changes would not bump it anyway.

### D5. Row markup: every non-RELEASED row is live

**In both row partials:**
- Drop `hx-get`/`hx-trigger`/`hx-swap`.
- Split today's `is_terminal` into `is_final = status == RELEASED`.
- **Every** displayed row, final or not, carries `data-key="<encode_cursor(created_at, id)>"`. The client takes `newest` from the first displayed row, which can be RELEASED (Show released, or after reconciliation rendered it RELEASED). Without a key there, the request would claim an empty section and raise a permanent false indicator (PR #512 review).
- Non-final rows also carry:
  - `sse-swap="booking-<id>"`, which is now also on READY/FAILED rows;
  - `data-row-version="<v>"`;
  - `data-live="inflight"|"settled"` for the client's class split.
- Non-final environment rows also carry the empty sync placeholder `<span id="environment-<id>-sync">`. When the row has more than M children, the placeholder is pre-filled with the D3a hint and `data-reconcile-skip`.
- A final row carries only `data-key`.

**Bookings and environments.** Environments use `derived_status` for the class.

**SSE load.** Adding `sse-swap` to READY/FAILED rows adds no server work. The stream already renders every authorized event, whether or not a row subscribes. Until now, the extend, label and release events of READY rows were rendered and then dropped by the client.

### D6. One poller and the indicator row live in the list section

The list-section partial gains:
- in `<thead>`, a second row `<tr id="bookings-new-rows">` (`environments-new-rows`), initially empty. It sits in the head, so `afterbegin` prepends into the tbody never land above it, and an OOB swap by id replaces it wherever it is;
- after the table, `<div class="hidden" id="bookings-reconcile" hx-get="<page_path>/reconcile?<filter_params>" hx-trigger="every 60s" hx-swap="none" hx-sync="this:drop" data-reconcile-max="50" data-reconcile-settled-min="10" data-rows="#bookings-list">`.

`hx-sync="this:drop"` drops a tick while a request is outstanding, so there is no overlap. The poller sits inside the section. Replacing the section therefore removes it, which stops its `every` timer, and adds exactly one new poller. `_rows_page`, the create response and single-row renders contain no poller. A test asserts there is exactly one `id="…-reconcile"` after appending pages.

The indicator, when shown, is a single cell that spans the table, holding a "Newer bookings available — refresh list" button. The button does `hx-get="<page_path>/list?<filter_params>" hx-target="#bookings-section" hx-swap="outerHTML"`, exactly the #494 filter-change path. It is an explicit user reset, never automatic.

### D7. Client: `app/static/js/row_reconcile.js` (~100 lines, no build step)

It listens at document level:

**`htmx:configRequest`, for pollers only:**
- Collect the `tr[data-live]` rows of the poller's `data-rows` tbody, in DOM order, excluding rows that contain `[data-reconcile-skip]` (D3a).
- Keep a per-poller rotation offset for each class (in-flight, settled) in a `WeakMap` keyed by the poller element, so a replaced section starts fresh.
- Take up to `max − min(settled.length, settledMin)` in-flight rows, rotating, then fill with settled rows, rotating. Because `settledMin ≥ 1` (D2), at least one settled row goes in every batch while any is displayed.
- Add `r=<id>.<version>` per row, and `newest=` from the `data-key` of the first `tr[data-key]` in the tbody, whatever its status. `newest` is omitted only when the tbody has no `tr[data-key]`.
- Record the sent `{id: version}` map on the request (`evt.detail.elt`'s pending map).

**`htmx:beforeSwap`, for pollers:** if the poller is no longer in the document (`!document.body.contains(elt)`), set `shouldSwap = false`. htmx then skips the whole swap, including the OOB rows. This is the "obsolete response" guard. Element identity is the section generation, so no token is needed.

**`htmx:oobBeforeSwap`:** if the swap comes from a reconcile response and the target row's current `data-row-version` differs from the version sent, cancel that row's swap. The row changed after the request was sent, so it is not overwritten by older data. Removals are not cancelled.

**Rotation under change.** Rotation offsets are indices into the class list at request time. When rows are added, removed or change class, the offset is clamped modulo the new length. A row may then be sent one tick early or late in that cycle. The bound in the spec is stated for a stable displayed set. The tests check it with a stable set, plus one test that a newly prepended row is reconciled within the bound computed with the new counts.

**Testing.** No JS test runner exists in the repo, and CI's Python job has no node. The rotation/selection logic is a pure function, `selectBatch(inflight, settled, max, settledMin, offsets)`, exported on `window.rowReconcile`. Its algorithm is mirrored by a small Python oracle in the test suite (`tests/reconcile_oracle.py`), which the convergence tests use (D9). A `node --test` file checks the JS against the same fixture table. It runs locally when node is present and is skipped otherwise. The real browser behaviour is checked manually against `docker compose up`, and the result is recorded in the PR.

*Alternatives considered:*
- **`hx-vals="js:…"`.** It needs `allowEval`, and it leaves stale-response and rotation state with no home.
- **A custom `fetch` + `htmx.process`.** That would re-implement htmx's OOB handling.

### D8. Newest-row probe: the first-page key walk with `limit=1`, keys only

The probe is **mandatory keys-only**: the phase-1 key statement of `list_page` alone (`_page_keys_stmt`, or `_label_page_keys_stmt` under a label filter, and the environments `_page_keys_stmt`), with `limit=1` and no cursor, under `_OrderedWalk`. It is exposed as `newest_key(session, *, scope…, filters…) -> (created_at, id) | None` on both repositories and ports.

`list_page(limit=1)` is **not** used. It adds the hydration statement, and for environments also the children read, which would break the D3 budget (PR #512 review). The statement-count test pins this.

The key walk uses the page's own scope and filters (Mine/All, types, label, Show released). That is exactly the first page's selection, already bounded by #479/#496:
- the label scan budget `BOOKINGS_LABEL_SCAN_SIZE` for bookings;
- the owner walks for environments.

Its single key is compared with `newest`. The indicator shows when the probe's key is greater than `newest`, by `(created_at, id)` order, or when `newest` is absent and the probe found a row.

Rows prepended by this tab carry their own `data-key` and become `newest`, so own orders are never signalled. Because of the label bound, a newer matching row hidden behind more than `BOOKINGS_LABEL_SCAN_SIZE` newer non-matching rows is not signalled. That is the same bound as the first page itself, and is acceptable.

Rows that are displayed but RELEASED still define `newest` (D5). A section whose first row was rendered RELEASED therefore does not trigger the indicator.

### D9. Convergence bound and its tests

The spec's formula follows from D7. Tests at the server level fix the inputs:
- 1 row;
- 50 rows;
- 150 rows across three pages.

They assert:
- one request per simulated tick;
- the batch size never exceeds `max`;
- statement counts are equal for 1 and 50 ids;
- in a simulated sequence of ticks, every row id is sent within the bound. This uses the Python oracle of `selectBatch` (D7), cross-checked against the JS by the local `node --test` fixture table.

## Risks / Trade-offs

- [An environment with more than M children is never reconciled] → It is visible, not silent: the row shows a reload hint and keeps its SSE subscription, and the default M = 20 is above any blueprint in practice. A test pins both the hint and the bounded read.
- [Settled rows on long lists converge slowly (minutes)] → Stated bound. SSE is still the fast path for managed rows. The indicator and reload reset everything.
- [All-list foreign rows now update for any viewer] → Only the list projection the All list already exposes, with the same actions and credential gating. A test pins that no extra field appears.
- [A missed version input causes a row never to refresh] → Bookings hash the whole projection by construction. Environments use an explicit child tuple with a per-field test.
- [The client offset drifts when rows churn] → Clamped. The bound is tested for stable sets and for one prepend case.
- [A long-open tab keeps old per-row polls after deploy] → The `/row` endpoints remain, so old tabs keep working until reload. During a rollback, new tabs' `/reconcile` would 404, and htmx ignores the 4xx. Rows then rely on SSE until reload.
- [Hidden poller element under `hx-sync=drop` on a hung request] → htmx's request timeout defaults to none. The poller sets `hx-request='{"timeout": 30000}'`, so a hung request frees the slot after 30 s.

## Migration Plan

There is no data migration, and no settings are required (defaults apply). Rollout is a normal deploy. Rollback reverts the commit: old templates poll `/row` again, and tabs opened with new HTML stop reconciling until reload.
