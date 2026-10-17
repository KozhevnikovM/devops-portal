## Why

Under high concurrency, scarce pooled resources (namespaces and static VMs) and multi-resource parent environments are vulnerable to race conditions such as duplicate allocation, lost updates, double releases, and invalid status transitions. While unit tests and mock-based tests verify isolated logic, PostgreSQL-backed integration tests executing across independent database sessions and row locks are needed to guarantee that allocation, promotion, and teardown invariants strictly hold under true concurrent access (#452, parent #433).

## What Changes

- Add a comprehensive PostgreSQL-backed integration test suite covering scarce resource concurrency:
  - **Concurrent allocation of the last free pooled resource:** Two concurrent booking requests race for the single remaining active pooled resource, parameterized for both namespaces and static VMs (both any-available and specific-selection paths). Tests verify that row-level locking (`FOR UPDATE SKIP LOCKED` / `FOR UPDATE`) ensures exactly one request acquires the resource and transitions to `READY`, while the other either enqueues (`QUEUED`) or raises an unavailable error (`NamespaceUnavailableError` / `StaticVMUnavailableError`), preventing duplicate ownership.
  - **Concurrent worker allocation and queue promotion:** Two concurrent background worker sessions run `promote_next_queued` for the same resource type (parameterized for both namespaces and static VMs) when a scarce resource frees up. Tests verify that row locking guarantees exactly one queued booking is promoted to `READY` with the resource assigned, while the other worker safely returns `None`.
  - **Concurrent releases for the same environment:** Two release requests run concurrently in separate database sessions for the same multi-resource environment. Tests verify that environment release is fully idempotent via per-booking atomic claims: children are released and torn down without duplicate Celery teardown dispatches, double promotions, or `IllegalStatusTransitionError` exceptions.
  - **Allocation racing with teardown/release:** A scarce pooled resource (namespace or static VM) is released or torn down concurrently with an allocation or queue promotion attempt. Tests verify that availability checks, row locks, and status transitions prevent lost updates, double allocation, or orphan states.
  - **Deterministic race orchestration:** Tests explicitly orchestrate overlapping transactions and lock contention using synchronization barriers, verify distinct backend connections via `pg_backend_pid()`, and enforce bounded waits/timeouts.
- Harden `ReleaseBookingUseCase` and `ReleaseEnvironmentUseCase` to use atomic claims / conditional updates on booking rows: only the winning transition emits side effects (dispatching teardown or promoting queued bookings). A booking already in `RELEASED` status (or `RELEASING` under force release) is a safe no-op that emits no side effects, while `FAILED` bookings preserve their required teardown cleanup.
- Verify core invariants: one resource cannot have two active owners simultaneously; quota and availability checks remain correct under concurrent writes; no double release or lost update occurs.

## Capabilities

### New Capabilities

- `scarce-resource-concurrency`: Concurrency control, mutual exclusion, and isolation invariants for scarce pooled resources (namespaces and static VMs), worker allocations, queue promotions, and allocation-release races.

### Modified Capabilities

- `environment-lifecycle`: Idempotent handling of concurrent environment releases to ensure child bookings are released and torn down safely without duplicate teardown dispatch or invalid transitions.

## Impact

- **Code:**
  - `app/application/use_cases/release_booking.py` and `app/infrastructure/repositories/booking_repo.py`: Implement per-booking atomic claim / conditional status update so only the winning transition emits teardown/promotion side effects. Bookings already `RELEASED` (and `RELEASING` under force release) return safely as no-ops without side effects, while `FAILED` bookings preserve required teardown/cleanup dispatch.
  - `app/application/use_cases/release_environment.py`: Ensure concurrent release calls for the same environment safely handle children without duplicate teardowns or promotion calls.
- **Tests:**
  - New integration test file: `tests/integration/test_scarce_resource_concurrency.py` (marked with `pytest.mark.integration` and `pytest.mark.postgres_integration`) exercising all four concurrency scenarios parameterized across namespaces and static VMs, with independent `AsyncSession` / database connections verified via `pg_backend_pid()` and synchronized barriers.
- **API / User-facing:**
  - No breaking API changes; concurrent requests to release environments or book scarce resources maintain predictable, safe behavior and invariants.
