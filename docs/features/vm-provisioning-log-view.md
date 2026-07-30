# Feature: Dedicated Terraform/Ansible provisioning log view — v0.13.0 F-7

**Issue:** #378

## Goal

Let a user open a booking's full Terraform/Ansible provisioning (and teardown) output in a
new browser tab, instead of a single long line dominating the booking row/list view. The
list view keeps a short, visually-contained snippet.

## Current state (verified)

- `status_message` (`Text`, nullable, `app/infrastructure/database/models.py:149`) is
  **overwritten** on every progress tick — nothing accumulates. `provision.py`/`teardown.py`'s
  `_on_progress` callbacks call `repo.sync_set_status_message(s, booking_uuid, msg)`, and every
  adapter/runner that produces `msg` already truncates to the last 3 lines itself
  (`"\n".join(lines[-3:])` in `app/infrastructure/terraform/vcd_adapter.py:165`,
  `app/infrastructure/config/ansible.py:169`, `app/infrastructure/config/runner.py:116`) — so
  there is no full log anywhere today, only a rolling 3-line window that's gone the moment the
  next line arrives.
- `partials/booking_row.html:46-47` renders the current `status_message` inline in an
  unconstrained `<pre class="text-xs text-gray-500 mt-1 whitespace-pre-wrap break-all">` — if
  a single captured line is long (e.g. a verbose Ansible debug-task dump), the wrapped render
  can visually dominate the row and push the rest of the page down, matching the reported "a
  single VM's log takes up the entire screen."
- The existing "Audit log" page (`GET /bookings/{id}/audit`,
  `app/presentation/routes/bookings.py:325-345`) shows the structured `STATUS_CHANGED` timeline
  (old→new status, timestamps) plus the current `status_message` — it's a different, already-
  useful view and stays exactly as-is; this feature adds a second, log-focused view alongside it.
- `run_script`/Ansible's `_run` call `on_progress` on **every non-empty output line**,
  unthrottled (`app/infrastructure/config/runner.py:113-116`,
  `app/infrastructure/config/ansible.py:163-169`) — i.e. the system already does a DB write per
  output line during script/Ansible phases; only Terraform's own progress (`vcd_adapter.py:164`)
  throttles to a 15s interval.

## Scope decision (resolved via discussion)

**Persist a real, bounded accumulated log**, not just a wider rolling window. A new
`bookings.provisioning_log` column accumulates output across the booking's lifecycle
(provisioning *and* teardown — one column, one "what actually happened to this booking,
technically" view, rather than a second column split by phase), capped at the last **50,000
characters** to bound growth. This is a genuine, if modest, DB migration — the only one in the
v0.13.0 plan.

## What changes

### `app/infrastructure/database/models.py` + new Alembic migration

`BookingModel` gains `provisioning_log: Mapped[str | None] = mapped_column(Text, nullable=True)`.
Nullable, no backfill — historical bookings simply show no log.

### `app/infrastructure/repositories/booking_repo.py`

New `sync_record_progress(session, booking_id, message: str) -> None`, replacing the
`sync_set_status_message` call sites in `_on_progress` callbacks (not the terminal
clear-to-`None` calls, which stay as plain `sync_set_status_message(None)` — a `None` doesn't
get appended to the log):

```python
def sync_record_progress(self, session: Session, booking_id: UUID, message: str) -> None:
    """Set the compact status_message (unchanged) and append to the capped provisioning_log,
    in one commit rather than two — this fires on every Ansible/script output line (#378)."""
    model = session.get(BookingModel, booking_id)
    if model is None:
        raise BookingNotFoundError(booking_id)
    model.status_message = message
    combined = (model.provisioning_log or "") + message + "\n"
    model.provisioning_log = combined[-50_000:]
    session.commit()
```

One round trip per call (same shape as every other `sync_*` write in this file — `session.get`
then mutate then commit), not two — avoids doubling write volume during the already-unthrottled
Ansible/script phases.

### `app/tasks/provision.py` / `app/tasks/teardown.py`

`_on_progress(msg)` calls `repo.sync_record_progress(s, booking_uuid, msg)` instead of
`repo.sync_set_status_message(s, booking_uuid, msg)`. The explicit `sync_set_status_message(s,
booking_uuid, None)` clear-calls (on success, before moving to the next phase) are unchanged.

### `app/presentation/routes/bookings.py`

New `GET /bookings/{booking_id}/log` (HTML, opens in a new tab), mirroring the existing audit
route's permission model exactly:

```python
@router.get("/bookings/{booking_id}/log", response_class=HTMLResponse)
async def booking_log_page(booking_id: UUID, request: Request, session=Depends(get_async_session),
                            current_user: User = Depends(require_user)):
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")
    return templates.TemplateResponse(request, "booking_log.html",
                                       {"booking": booking, "current_user": current_user})
```

Same owner-or-admin check as `/bookings/{id}/audit` — not more restrictive, not less.

### New `app/presentation/templates/booking_log.html`

A minimal standalone page (extends `base.html`) rendering `booking.provisioning_log` in a
monospace, pre-wrapped block, with a "no log yet" placeholder when empty.

### `app/presentation/templates/partials/booking_row.html`

The inline `status_message` `<pre>` gets height-contained instead of unbounded:
`max-h-28 overflow-y-auto` (roughly 5 lines' worth, matching the issue's explicit ask), so a
long wrapped line scrolls within its own box instead of pushing the rest of the row/page down.
A "View full log ↗" link (`target="_blank"`, `href="/bookings/{{ booking.id }}/log"`) appears
next to it whenever `booking.provisioning_log` is non-empty.

## Expected behaviour / edge cases

- A booking with no provisioning activity yet (e.g. still `PENDING`) shows nothing new — no
  log link, no change to the row's current empty state.
- A long-running Ansible phase no longer visually dominates the row/page — the snippet scrolls
  within a fixed height; the full log is one click away in a new tab.
- The full log is capped at 50,000 characters — an extremely long-running or chatty
  provisioning run will have its oldest output silently trimmed from the front, same tradeoff
  already accepted by the *existing* 3-line rolling window, just with a much larger, genuinely
  useful window instead of effectively nothing retained.
- Historical bookings (created before this migration) show "no log yet" on the new page —
  `provisioning_log` is nullable with no backfill.
- Teardown output (including a future re-dispatched forced teardown, e.g. from #374's fix)
  appends to the same log — directly useful for diagnosing a stuck-release booking, since the
  full destroy output is now visible instead of only the last 3 lines.
- No JSON API equivalent in this pass (the issue is specifically about the browser UI); a
  `GET /api/bookings/{id}/log` twin, matching the audit log's existing HTML+JSON pair, is a
  natural but unscoped follow-up if API/CI consumers need it later.

## DB Migrations

One new column: `bookings.provisioning_log` (`Text`, nullable). No backfill, no data migration.

## API Changes

New `GET /bookings/{booking_id}/log` (HTML only, this pass). No change to any existing route.

## Testing Plan

- `sync_record_progress`: unit test asserting both `status_message` and `provisioning_log` are
  updated in one call, and that `provisioning_log` is capped (feed >50,000 chars, assert the
  stored value's length and that it holds the *tail*, not the head).
- `provision.py`/`teardown.py`: confirm (via existing task tests, patched repo) that
  `_on_progress` now calls `sync_record_progress` instead of `sync_set_status_message`, and
  that the terminal clear-to-`None` calls are unchanged.
- `GET /bookings/{id}/log`: route tests mirroring the existing audit-route tests — 200 for
  owner/admin, 403 for a non-owner non-admin, 404 for an unknown booking id.
- Full suite regression (`pytest tests/ -m "not integration"` + integration tier).
