# Feature: `CONFIGURING` booking state + provision-task config seam (v0.8.0 P1.1)

## Goal

Lay the foundation for post-provision VM configuration (bash scripts in P1.2, Ansible roles in
P2.2) by adding a new booking lifecycle state and the seam in the provision task where
configuration will run — **without changing behaviour for any current booking** (nothing is
configurable yet, so the seam is a no-op today).

New lifecycle:

```
PENDING → PROVISIONING → CONFIGURING → READY / FAILED
```

`CONFIGURING` is entered **only** when a booking has something to configure. Until P1.2/P2.2 add
configurable fields, the predicate is always false, so every VM goes `PROVISIONING → READY` exactly
as it does now.

## What changes

### Domain
- `BookingStatus.CONFIGURING = "CONFIGURING"`. The `status` column is `String(32)` and the audit
  `old/new_status` columns are `String(32)`, so `CONFIGURING` (11 chars) fits — **no migration**.

### Provision task (`app/tasks/provision.py`)
- After `terraform apply` returns the IP, route through a config seam instead of going straight to
  `READY`:
  - `_needs_configuration(booking)` → returns `False` for now (no configurable fields exist yet).
  - When it returns `True` (P1.2+): set `CONFIGURING` (+ status message/audit), run the injected
    config runner, then `READY`. A config failure raises and is handled by the existing
    retry/`FAILED` path.
  - When `False`: transition straight to `READY` as today.
- The config runner is a small injected collaborator (a no-op default in this item) so P1.2 can
  drop in the SSH executor without touching the task's control flow.

### Lifecycle classification (so the new state behaves correctly everywhere)
- **UI** (`partials/booking_row.html`): `CONFIGURING` is **non-terminal** (`is_terminal` stays
  `READY`/`FAILED`/`RELEASED`), so the row keeps polling; add it to the animated in-progress badge
  list and add a `status-CONFIGURING` style alongside the other in-flight statuses.
- **Release** (`ReleaseBookingUseCase`): treat `CONFIGURING` like `PROVISIONING` — an in-flight
  state that a normal owner release rejects (`409`) but an **admin can force-delete** (the VM
  exists in VCD, so teardown destroys it). Add `CONFIGURING` to `_FORCE_DELETABLE_STATUSES` /
  `_IN_FLIGHT_STATUSES`.
- **Stale reaper** (`reap_stale_provisioning` beat task): include `CONFIGURING` so a booking that
  somehow stalls in configuration is still swept to `FAILED` after the threshold. (P1.2 adds a
  dedicated SSH/Ansible timeout; this is the defensive backstop.)

No API shape change (no new request/response fields in this item); no DB migration.

## Expected behaviour

- Every existing VM booking: unchanged — `PROVISIONING → READY` (the seam's predicate is `False`).
- The enum, UI badge, release classification, and reaper now **recognise** `CONFIGURING`, so when
  P1.2 starts entering the state the rest of the system already treats it correctly.

## Tests

- Enum: `CONFIGURING` exists; the non-terminal/in-flight/force-deletable membership sets contain it
  as specified.
- Provision task: with `_needs_configuration` stubbed `True`, the task transitions
  `PROVISIONING → CONFIGURING → READY` and invokes the (stub) config runner once; with the default
  `False`, it goes `PROVISIONING → READY` and never enters `CONFIGURING` (regression guard that
  existing VMs are unaffected).
- Release: an admin can force-delete a `CONFIGURING` booking (→ teardown); a non-owner/owner normal
  release gets `409`.
