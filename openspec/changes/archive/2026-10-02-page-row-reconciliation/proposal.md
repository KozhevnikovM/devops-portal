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
- The rows are read through the existing list-safe projection: one batch read by id, at most one queue-rank read (#495 helper), and one keys-only newest-row probe through the existing first-page key walk. There are no form-catalog reads and no per-row queries.
- The statement count is fixed whatever the batch size: at most 7 for a bookings page and 5 for environments, counting the plan-pin statements.
- Environment children are bounded by a new write-side invariant, `ENVIRONMENT_MAX_CHILDREN` (default 25):
  - blueprint saves (admin page and JSON API) and environment orders over the limit are rejected;
  - legacy live environments raise the per-process effective limit `C_eff` at startup instead of being excluded.

  The reconcile children read stops after `C_eff + 1` children per environment, so every displayed environment of application-valid data is reconciled in full and converges. If the invariant is broken by a direct database edit, reconciliation fails closed for that environment with a "reload required" row and does no unbounded read.

**Bounded, stated convergence**
- The client rotates through the displayed rows in batches. In-flight rows go first, and a reserved share of `min(S, RECONCILE_SETTLED_MIN)` slots per batch goes to settled (READY/FAILED) rows, where S is the number of settled rows displayed. The reserve is at least 1, so settled rows are never starved.
- The worst-case delay before a change shows up is a stated function of the number of displayed rows, the batch size and the interval. A row converges within one interval only while its class fits in a single batch.

**Visibility separate from management**
- Each requested id is re-authorized server-side against **list visibility** for the page's scope: Mine = owner or creator; All = any row of the page's resource types, which is what the All list already shows.
- An unknown id and an id outside that scope get the same removal directive, so a forged id reveals nothing.
- Rows are rendered with the list's projection and the existing action and "Show credentials" rules. No credential or other secret is ever embedded.

**Convergence rules**
- Every row that is not RELEASED is live: it keeps or gains `sse-swap` and is included in reconciliation. This adds READY and FAILED rows, so READY → RELEASING now converges.
- A RELEASED row is rendered once in its final state and then leaves reconciliation.
- Rows are never removed for no longer matching the label or Show released filter. They are removed only when deleted or not visible.
- Newer matching rows are detected with one bounded first-page probe, compared with the list key of the first displayed row. Every displayed row carries its key, RELEASED rows included. It shows an indicator that reloads the list section on click. Loaded pages are never replaced silently.

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
- `environment-lifecycle`: environments have a bounded number of children:
  - blueprints and orders over `ENVIRONMENT_MAX_CHILDREN` are rejected;
  - legacy live environments set the effective limit at startup.

  This is a **behaviour change for admins**: a blueprint larger than the limit can no longer be saved or ordered until the setting is raised.

## Impact

- **Code**:
  - `app/presentation/routes/bookings.py` and `environments.py` gain the reconcile routes.
  - A small shared presentation helper computes row versions and parses and validates the reconcile parameters.
  - `booking_repo` / `environment_repo` expose their existing phase-2 hydration (list projection by ids) as a public batch read, also declared on the ports.
  - Templates: the row partials drop `hx-get`/`hx-trigger`, gain `data-row-version`/`data-key`, and keep `sse-swap` on every non-RELEASED row. The list-section partials gain one poller element and a new-rows indicator row.
  - A new static `row_reconcile.js` (about 100 lines) builds the batch and guards against stale responses.
  - `app/config.py` gains `RECONCILE_MAX_IDS` (default 50, validated ≤ page size), `RECONCILE_SETTLED_MIN` (default 10, validated `1 ≤ R < RECONCILE_MAX_IDS`) and `ENVIRONMENT_MAX_CHILDREN` (default 25, ≥ 1).
  - The blueprint save routes (admin and JSON) and `order_environment` call a new domain size validator. `app/main.py`'s `lifespan` computes `C_eff`. The 60 s interval stays a template constant, as today.
  - `app/main.py`'s access-log filter also suppresses the reconcile path.
- **HTTP surface**: three new HTML-only GET routes (`include_in_schema=False`, `require_user`). The JSON API is unchanged.
- **Load**: per-tab background requests drop from one per non-terminal row per minute to one per list section per minute. Per-request DB work is a fixed statement count (≤ 7 / ≤ 5), with rows bounded by `RECONCILE_MAX_IDS` and environment children by `RECONCILE_MAX_IDS × (C_eff + 1)`.
- **Tests**: new unit/API tests cover caps, authorization (forged and foreign ids under Mine/All), changed-only responses, removals, the probe and the template contract. An integration test counts statements for 1 row, 50 rows and several loaded pages. A small JS-level rotation test, or a documented manual browser check, covers the client. Existing tests asserting `hx-trigger="every 60s"` are updated.
- **Docs**: `docs/api-reference.md` (row endpoints, `/events/stream`, new reconcile fragments) and `docs/admin-guide.md` (the nginx/SSE note and the rolling-deploy note).
