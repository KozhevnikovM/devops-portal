## Why

Every non-terminal booking and environment row runs its own `hx-get …/row` every 60 s (`partials/booking_row.html`, `partials/environment_row.html`). This is the fallback for SSE, because Redis pub/sub has no replay. Background traffic therefore grows with the number of displayed active rows times the number of open tabs, and Load more can stack up many pages of polling rows (#497, parent #439).

The per-row model also has three correctness gaps:
- **No refresh for READY or FAILED rows.** They carry neither the SSE subscription nor the poll, so READY → RELEASING (TTL expiry, release from another tab, admin action) never reaches an open page until reload.
- **Useless polls on All.** On the All list, other users' rows poll an endpoint that answers 403 for them. They never update but keep polling forever.
- **No sign of new rows.** Nothing tells the user that newer matching rows exist.

The dependencies are in place. #494 provides the list-section fragment contract, and #495 provides the single-statement queue-rank read. This change can therefore replace the per-row timers with one bounded request per page.

## What Changes

**Page-level reconciliation**
- Replace the per-row `hx-trigger="every 60s"` fallback polls with **one page-level reconciliation request per 60 s interval** on each bookings page (VM, namespace) and the environments page. SSE stays the fast path, and nothing depends on SSE delivery for eventual consistency.
- Add an HTML-only reconciliation endpoint per list page: `GET /book/vm/reconcile`, `GET /book/namespace/reconcile` and `GET /environments/reconcile`. Each request carries:
  - the page's filters (Mine/All, label, Show released);
  - a batch of displayed row ids, each with the row version the client holds;
  - the key of the newest displayed row.

  The response contains out-of-band swaps of only the rows whose state changed, removal directives for rows that no longer exist or are not visible in the page's scope, and a "newer rows available" indicator.

**Server-enforced bounds**
- At most `RECONCILE_MAX_IDS` ids per request (default 50, the page size). An oversized, duplicated or malformed id list is rejected with 400, so a client cannot exceed the cap.
- The rows are read through the existing list-safe projection: one batch read by id, at most one queue-rank statement (#495 helper), and one bounded newest-row probe through the existing first-page walk. There are no form-catalog reads and no per-row queries.

**Bounded, stated convergence**
- The client rotates through the displayed rows in batches. In-flight rows go first, and a reserved share of each batch goes to settled (READY/FAILED) rows.
- The worst-case delay before a change shows up is a stated function of the number of displayed rows, the batch size and the interval. A row converges within one interval only while its class fits in a single batch.

**Visibility separate from management**
- Each requested id is re-authorized server-side against **list visibility** for the page's scope: Mine = owner or creator; All = any row of the page's resource types, which is what the All list already shows.
- An unknown id and an id outside that scope get the same removal directive, so a forged id reveals nothing.
- Rows are rendered with the list's projection and the existing action and "Show credentials" rules. No credential or other secret is ever embedded.

**Convergence rules**
- Every row that is not RELEASED is live: it keeps or gains `sse-swap` and is included in reconciliation. This adds READY and FAILED rows, so READY → RELEASING now converges.
- A RELEASED row is rendered once in its final state and then leaves reconciliation.
- Rows are never removed for no longer matching the label or Show released filter. They are removed only when deleted or not visible.
- Newer matching rows are detected with one bounded first-page probe. It shows an indicator that reloads the list section on click. Loaded pages are never replaced silently.

**Client guarantees**
- No overlapping reconciliation requests, and one timer per list section.
- A response for a replaced section, for example after a filter change in flight, is ignored.
- A row that changed after its request was sent (for example through SSE) is not overwritten by an older response.
- Loaded pages, order, filters, row actions and the Load more cursor are preserved.

**Unchanged and incidental**
- `GET /bookings/{id}/row` and `GET /environments/{id}/row` stay as they are, with owner/creator/admin authorization. No row timer calls them any more.
- Documentation of the 60 s per-row fallback in `docs/api-reference.md` and `docs/admin-guide.md` is replaced.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `live-row-updates`:
  - The reconnect/Redis-outage/rolling-deploy guarantees now rest on page-level reconciliation, not per-row fallback polls. The requirement "Scoped delivery keeps the existing failure and reconnect behaviour" is modified accordingly.
  - New requirements cover: the bounded reconciliation request and its server caps; list-visibility re-authorization; changed-rows-only responses with removal directives; the convergence bound; non-RELEASED rows being live; new-row detection; and client request hygiene (no overlap, stale responses ignored).
- `booking-listing`: listed booking rows carry no per-row timer, each carries a row version and a list key, and every bookings list section carries exactly one reconciliation poller.
- `environment-listing`: the same for environment rows and the environments list section.

## Impact

- **Code**:
  - `app/presentation/routes/bookings.py` and `environments.py` gain the reconcile routes.
  - A small shared presentation helper computes row versions and parses and validates the reconcile parameters.
  - `booking_repo` / `environment_repo` expose their existing phase-2 hydration (list projection by ids) as a public batch read, also declared on the ports.
  - Templates: the row partials drop `hx-get`/`hx-trigger`, gain `data-row-version`/`data-key`, and keep `sse-swap` on every non-RELEASED row. The list-section partials gain one poller element and a new-rows indicator row.
  - A new static `row_reconcile.js` (about 100 lines) builds the batch and guards against stale responses.
  - `app/config.py` gains `RECONCILE_MAX_IDS` (default 50, validated ≤ page size) and `RECONCILE_SETTLED_MIN` (default 10, validated < `RECONCILE_MAX_IDS`). The 60 s interval stays a template constant, as today.
  - `app/main.py`'s access-log filter also suppresses the reconcile path.
- **HTTP surface**: three new HTML-only GET routes (`include_in_schema=False`, `require_user`). The JSON API is unchanged.
- **Load**: per-tab background requests drop from one per non-terminal row per minute to one per list section per minute. Per-request DB work is bounded by `RECONCILE_MAX_IDS` and is at most a list page's worth of statements.
- **Tests**: new unit/API tests cover caps, authorization (forged and foreign ids under Mine/All), changed-only responses, removals, the probe and the template contract. An integration test counts statements for 1 row, 50 rows and several loaded pages. A small JS-level rotation test, or a documented manual browser check, covers the client. Existing tests asserting `hx-trigger="every 60s"` are updated.
- **Docs**: `docs/api-reference.md` (row endpoints, `/events/stream`, new reconcile fragments) and `docs/admin-guide.md` (the nginx/SSE note and the rolling-deploy note).
