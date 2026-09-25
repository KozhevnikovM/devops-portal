## Context

The motivation is in `proposal.md` (Why), and the required behaviour is in `specs/environment-lifecycle/spec.md`. The current state that shapes the approach:

- `ReleaseBookingUseCase.execute(..., force=False)` serves both the HTMX route and the JSON booking-release routes. Its only `force=True` caller is `ReleaseEnvironmentUseCase`. Both routes already map any `BookingError` to `409`.
- `enforce_environment_ttl` does not go through the use case. It uses its own sync `_release_child_sync`.
- `_derived_status()` lives in `app/presentation/routes/api_environments.py` and is imported by `routes/environments.py`. It is pure logic sitting in the presentation layer.
- The "all children READY" lease rule is written twice in `environment_repo.py`: the async `start_lease_if_ready` and the sync `_stamp_lease_if_all_ready`. It is triggered at order time (the async path) and when a VM child reaches READY in `provision.py` (the sync path).
- Environment children can be QUEUED (`reserve_pooled_resource._enqueue`). `_assign_resource_and_ready` stamps only the booking's own lease when a queued child is promoted, and never checks the environment. So a queued namespace child promoted last also leaves the environment on its placeholder expiry.
- The placeholder expiry is `PERMANENT_EXPIRES_AT`. `ttl_minutes == 0` also maps to `PERMANENT_EXPIRES_AT`.

## Goals / Non-Goals

**Goals:**
- One enforcement point for the child-release rule, in the application layer.
- One domain definition each for the derived environment status and the lease-start rule, shared by the async and sync paths and by both presentation routers.

**Non-Goals:**
- Changing admin **Force release** (`ForceReleaseBookingUseCase`). It accepts only FAILED or stuck-RELEASING VMs and remains the recovery tool for a child whose teardown failed during an environment release. A child it force-releases makes the environment `FAILED` (partly released), and the environment Release action stays available.
- Per-child extend, relabel or other child operations.
- A new environment status value such as `DEGRADED`. This was decided against: the existing `FAILED` is reused.
- A dedicated data migration or a way to tell environments stuck before the deploy apart from gaps after it. Repair is retroactive through reconciliation (D4, "Retroactive repair"); no separate mechanism is needed.

## Decisions

### D1. Guard in `ReleaseBookingUseCase`, bypassed only by the environment release

After the booking is loaded and the permission check (`can_manage`) passes, if `booking.environment_id is not None` and `force` is false, raise `EnvironmentChildReleaseError(BookingError)` with the message `Booking belongs to environment <id>; release the environment instead.` The check runs *after* the permission check, so a non-owner still gets `403` and learns nothing about the environment. It runs *before* the QUEUED-cancel branch, so a queued child can't be cancelled on its own either.

`force=True` stays the environment-release path. It is the only caller today, and its docstring already describes it as "used when releasing a whole environment". The error subclasses `BookingError`, so both routes return `409` with no route change. Tests pin that mapping.

- *Alternative:* a separate `via_environment` flag. Rejected because it adds a second parameter with the same meaning as `force` and gains nothing.
- *Alternative:* enforce only in the routes. Rejected, because the issue explicitly requires application-layer enforcement.

### D2. Derived status becomes a domain function

Add `derive_environment_status(statuses) -> BookingStatus` to `app/domain/` (for example `environment_status.py`). It implements the ordered rules in the spec: empty → READY; any FAILED → FAILED; any in flight (QUEUED, PENDING, PROVISIONING, CONFIGURING, RETRY) → PROVISIONING; all RELEASED → RELEASED; all READY → READY; anything else → FAILED. `_derived_status` in `api_environments.py` becomes a thin wrapper that returns `.value`, so the two routers keep their import.

This moves pure business logic out of the presentation layer. It also turns the fall-through into an explicit "all READY" check, so any mix the rules don't list lands on FAILED instead of READY.

- *Alternative:* keep it in presentation and patch only the last branch. Rejected because the rule belongs in the domain and is easier to unit-test there.

### D3. Lease-start rule: settled plus at least one READY, started once

Add a domain constant, `CAN_BECOME_READY` in `app/domain/booking_status.py`: the statuses from which READY is reachable in `ALLOWED_TRANSITIONS`, computed as a transitive closure rather than listed by hand. It evaluates to QUEUED, PENDING, PROVISIONING, CONFIGURING and RETRY. Computing it keeps the "in flight" definition from drifting away from the transition graph.

Add `lease_can_start(statuses) -> bool` to `app/domain/lease.py`. It returns true when there is at least one child, no child's status is in `CAN_BECOME_READY`, and at least one child is READY.

**RELEASING does not block the lease (decided).** RELEASING can only lead to RELEASED or FAILED, so it is not in flight. If it did block, a child stuck in RELEASING (for example a teardown worker that died; RELEASING has no stale reaper) would keep its READY siblings on the placeholder expiry for good, which is the #434 failure again. Treating it as settled also means the teardown task needs no lease trigger. The alternative, RELEASING blocks, was rejected for exactly that reason.

Both `start_lease_if_ready` and `_stamp_lease_if_all_ready` call `lease_can_start` instead of their own inline copies. Both also skip the stamp when the environment's `expires_at` is no longer `PERMANENT_EXPIRES_AT` while `ttl_minutes > 0`, which means the lease has already started.

**Serialization.** Every stamp path, sync and async, starts by locking the environment row (`SELECT … FROM environments WHERE id = :id FOR UPDATE`). Only under that lock does it read the children's statuses and the environment's `expires_at`, evaluate `lease_can_start` and the already-started guard, stamp, and commit, which releases the lock. A concurrent trigger blocks on the lock. Once it gets the lock, it runs a fresh statement under READ COMMITTED, sees the committed deadline, and does nothing. Each trigger commits its own child's status change *before* it calls the check (the tasks already use a separate short-lived `_run` session per operation), so the lock holder always sees every settled child.

- *Alternative:* an atomic compare-and-set (`UPDATE environments SET expires_at = :d WHERE id = :id AND expires_at = :sentinel`, then stamp the children only if the row count is 1). Rejected because the children's `expires_at` writes would not be covered by the check, and because `FOR UPDATE` is already the project's pattern for serializing a read-check-write (quota enforcement, `test_quota_concurrent_writes.py`). That makes every trigger idempotent and keeps a later terminal event from moving the deadline. For `ttl_minutes == 0`, stamping writes `PERMANENT_EXPIRES_AT` again, which is harmless.

Requiring at least one READY child avoids starting a lease for a stack with nothing live. For example, when every child FAILED, `enforce_environment_ttl` would find no live child anyway.

**Triggers.** The existing ones stay: order time (async) and a VM child reaching READY in `provision.py`. Three are added:
- a VM child reaching final FAILED in `provision.py` (the last-attempt branch and the non-retryable `SecretDecryptionError` branch);
- `reap_stale_provisioning` marking a child FAILED;
- queued-child promotion (`promote_next_queued` / `sync_promote_next_queued`) when the promoted booking has an `environment_id`.

Each added trigger calls the existing `sync_start_lease_if_ready_for_booking(booking_id)` or its async equivalent. That function is already a no-op for standalone bookings. For promotion, the repository promotion method runs the check **after its own commit, in a separate transaction**, not inside the promotion transaction. The promotion method is the only place that knows which booking was promoted, so the call still goes there. Running it inside the promotion transaction would risk a lock-order deadlock: the promotion holds the promoted child's booking row (`FOR UPDATE SKIP LOCKED`) and would wait for the environment row, while a concurrent stamper holds the environment row and waits to write that child's `expires_at`. The async promotion path needs an async counterpart, `start_lease_if_ready_for_booking`.

These triggers keep the lease timely, but they are not the correctness guarantee. A crash between a settling commit and its trigger is covered by periodic reconciliation (D4).

- *Alternative:* keep "all READY" and rely on the hardened status plus a manual release. Rejected, because the issue explicitly asks that a terminal child must not leave the placeholder in place forever.
- *Alternative:* start the lease at order time. Rejected, because it reintroduces #223 (provisioning time eaten out of the lease).

### D4. Periodic lease reconciliation is the crash-safe guarantee

Every immediate lease trigger runs in a separate transaction *after* the settling commit it follows:

- `provision.py` commits READY or final FAILED in one `_run` session and calls the lease check in the next one. This is already true today for READY.
- `reap_stale_provisioning` does the same.
- Promotion does the same (D3).

A crash in that gap leaves the environment on the placeholder expiry, and nothing else would ever stamp it. Rather than making each trigger atomic separately, one reconciliation path covers all of them. This mirrors the project's SSE design, a fast push with a slow polling fallback.

- New repository query `EnvironmentRepository.sync_list_lease_pending(session)`. It returns environments with `construction_complete` (D6), `expires_at = PERMANENT_EXPIRES_AT`, `ttl_minutes > 0`, at least one READY child and no child whose status is in `CAN_BECOME_READY`. The query only preselects candidates; the authoritative check is re-run under the row lock.
- New beat task `reconcile_environment_leases` in `app/tasks/beat_tasks.py`, scheduled every `ENFORCE_TTL_INTERVAL_SECONDS` next to `enforce_environment_ttl`. For each candidate it calls the same locked start-once path from D3, one short-lived session per environment, and logs and continues if one environment fails. Because that path locks, re-checks and is idempotent, a race between reconciliation and a live trigger still produces exactly one deadline.
- **Retroactive repair (decided).** The query does not tell a lease missed after the deploy apart from an environment stuck before it. After the deploy, the first reconciliation run starts the lease for every existing environment that meets the rule, for example one that was already `READY + RELEASED` (the #434 orphan), `READY + FAILED` or `READY + RELEASING`. That lease is `now + ttl_minutes` measured from that run, not backdated, so the owner gets the full TTL from the deploy before `enforce_environment_ttl` tears down the remaining live children. Environments the rule does not cover are left exactly as before: no READY child (nothing live left to orphan), a child still in flight, or `ttl_minutes == 0` (permanent by choice). This is intended, because these environments are the leaked resources #434 is about.
  - *Alternative:* skip environments that were stuck before the deploy, using a cut-over timestamp or a marker column. Rejected: it adds a schema change or a special case, and it would leave the exact resources #434 describes leaking forever.
- The immediate triggers stay, so in the normal case the lease starts at the settling event rather than up to one interval later. The #223 promise ("the lease grants ttl_minutes of usable time") is preserved. After a crash, the stack gets up to one interval of extra time, which is accepted.

- *Alternative:* keep promotion and lease start atomic in one transaction with a fixed lock order (environment row, then child rows). Rejected for two reasons. It fixes the promotion gap only, and the provision and reaper gaps would each need their own restructuring. It is also awkward to guarantee: promotion picks the queued booking with `FOR UPDATE SKIP LOCKED` before it knows the booking's environment, so it would have to release that lock and re-acquire in environment-first order, or retry.
- *Alternative:* start the lease only from reconciliation. Rejected because it adds up to one interval of latency to every environment in the normal case.

### D5. UI: replace Release with an environment hint

In `booking_row.html`, wrap every action that calls `hx-delete="/bookings/{id}"` in `{% if not booking.environment_id %}`. That is Release (READY/FAILED), the admin Delete (in flight) and Cancel (QUEUED); each would always return 409 for an environment child. For an environment child, render a non-interactive "Managed by environment" line. Link it to the environment if the row can build that link, `/environments#environment-<id>` or the environments page filtered to that id, without an extra query. Admin **Force release** keeps its current condition (see Non-Goals).

### D6. Construction-complete marker: nothing stamps a half-built environment

`OrderEnvironmentUseCase` creates the environment row and then its children one at a time. Every child path commits on its own: `BookingRepository.create`, pooled reservation, namespace adoption (`set_environment`), and the quota row lock. So a partly built environment is visible to other sessions. Once its first READY pooled child (or an adopted READY namespace) is committed, the environment looks settled: it has one READY child and none in flight, because the later children don't exist yet. A reconciliation tick, or a queued-child promotion triggered by another session, could then start the lease early. Later children would get a different expiry, and the order's final `start_lease_if_ready` would refuse to move the lease that had already started.

- New column `environments.construction_complete BOOLEAN NOT NULL`. Migration `0032` adds it with `server_default true`, so every existing row counts as constructed and the retroactive repair (D4) still applies to them. It then sets the server default to `false`, so any row inserted without an explicit value starts incomplete. The ORM default is also `False`.
- `EnvironmentRepository.create` inserts with `construction_complete = False`. A new `EnvironmentRepositoryPort.mark_construction_complete(session, environment_id)` sets it to `True` and commits. `OrderEnvironmentUseCase` calls it after the last child has been created, including an adopted namespace, and before its `start_lease_if_ready`. The rollback path never calls it; the environment row is deleted anyway.
- `_stamp_lease` refuses while `construction_complete` is false. That one guard, in the shared locked path, covers every trigger: reconciliation, promotion, provision READY or FAILED, and the reaper. `sync_list_lease_pending` also filters on the column, to keep the candidate set small.
- *Alternative:* make the parent and all child creation atomic, with no intermediate commits. Rejected: the order reuses child use cases that each commit (booking create, pooled reserve, adoption, quota `FOR UPDATE`), and its best-effort rollback is built around those commits. Making the order atomic would change the quota-locking and rollback semantics across several use cases. That refactor is larger than, and separate from, #434.
- *Alternative:* store an expected child count and compare it with the children that exist. Rejected: it duplicates what the flag says less directly, and an adopted namespace and a blueprint item are counted differently.
- *Alternative:* a time-based grace period. Rejected: a slow order (quota locks, a DB stall) can exceed any grace period, so it is not a correctness guarantee.

## Risks / Trade-offs

- [API clients that release environment children directly now get `409`] → This is intentional: the behaviour is **BREAKING**, it is documented in `docs/api-reference.md`, and the message tells the caller which environment to release.
- [Starting the lease with a FAILED child means the TTL can tear down READY siblings while someone is still investigating the failure] → The owner can release or order again. Leaving the resources running forever is the worse failure (#434). The admin guide will describe the rule.
- [Extra triggers on the promotion path add work after promotion] → One environment lookup and a short row lock, only when `environment_id` is set, and outside the promotion transaction, so the promotion's own locks are never held while waiting. The sync/async split is kept.
- [The environment row lock adds contention] → It is held only for one children read plus the stamp. It is taken only by lease triggers, not by ordinary booking operations. Nothing takes locks in the opposite order (child row, then environment row) while holding them, because promotion runs its check after committing.
- [A crash between a settling commit and its immediate lease check] → This is covered by reconciliation (D4). The immediate triggers only reduce latency; reconciliation guarantees the lease starts. The worst case is a delay of one beat interval (`ENFORCE_TTL_INTERVAL_SECONDS`) before the lease starts.
- [Reconciliation adds a periodic query] → One indexed scan for environments that still have the placeholder expiry. That set is small: only stacks currently being provisioned or recently settled.
- [`FAILED` is used both for "a child failed" and "partly released"] → It is accepted as a simplification. In both cases the action is the same: release the environment.
- [On the first reconciliation run after the deploy, environments that were already stuck (live READY children on the placeholder expiry) get a lease, and their remaining resources are torn down one TTL later. Someone may be relying on such a stack without knowing it was supposed to have a TTL.] → This is intended (D4). The lease is measured from the deploy, so every owner gets a full TTL of notice. The release notes and `docs/admin-guide.md` call it out. Before deploying, an operator can list the affected environments with the same criteria as `sync_list_lease_pending`, the query documented in the admin guide. An owner can then release the environment or re-order it.

## Migration Plan

One additive schema migration, `0032`: `environments.construction_complete`, with existing rows backfilled to `true` (D6). There is no data migration beyond that backfill.

1. Optional, before the deploy: list the environments that will be repaired, using the admin-guide query (placeholder expiry, `ttl_minutes > 0`, at least one READY child, no child that can still become READY), and warn their owners.
2. Deploy. The first `reconcile_environment_leases` run, within one `ENFORCE_TTL_INTERVAL_SECONDS`, starts a lease of `now + ttl_minutes` for each of them. Those stacks are torn down one TTL after the deploy unless their owners act.
3. Rollback: reverting the code and downgrading `0032` (which drops the column) restores the old behaviour for new events. Leases already started stay in place, which is correct because the stacks are still bounded by them.
