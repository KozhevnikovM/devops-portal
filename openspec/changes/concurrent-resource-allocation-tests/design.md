## Context

Scarce resources in the portal comprise pooled resources (namespaces and static VMs) and multi-booking parent environments. Allocation and promotion rely on PostgreSQL row locking (`FOR UPDATE` for specific resources, `FOR UPDATE SKIP LOCKED` for any-available allocation and queue promotions). While unit tests mock repository ports, issue #452 (parent #433) requires Postgres-backed race-condition tests executing against independent database connections to verify that locking and status guards prevent duplicate ownership and double releases under real concurrency.

See `proposal.md` for background and `specs/scarce-resource-concurrency/spec.md` and `specs/environment-lifecycle/spec.md` for requirements.

## Goals / Non-Goals

**Goals:**
- Provide automated, PostgreSQL-backed race-condition integration tests covering:
  1. Two bookings concurrently requesting the last free pooled resource (parameterized for both namespaces and static VMs, across any-available and specific-selection paths).
  2. Two workers concurrently allocating/promoting the same scarce resource (for both namespaces and static VMs).
  3. Two releases running concurrently for the same environment with mixed child resources.
  4. Allocation racing with teardown/release of a pooled resource (namespaces and static VMs).
- Enforce core invariants: single active ownership of pooled resources, correct availability/queue behavior, and no duplicate releases, duplicate queue promotions, or lost updates.
- Ensure `ReleaseBookingUseCase` uses an atomic claim mechanism on the booking row so that only the winning transition emits teardown dispatch or queue promotion side effects.
- Orchestrate deterministic race tests with overlapping transactions, backend PID verification, and bounded timeouts.

**Non-Goals:**
- Schema changes or database migrations: existing PostgreSQL constraints and row-level locking mechanisms (`FOR UPDATE`, `FOR UPDATE SKIP LOCKED`) are sufficient.
- End-to-end browser or Locust HTTP load testing (covered by `load-testing`).
- Altering the domain transition graph or status machine rules.

## Decisions

### Decision 1: Concurrency driver, session isolation, and deterministic race orchestration
- **Choice:** Orchestrate overlapping transactions across independent database sessions:
  - Each competing operation runs in its own `AsyncSession(async_engine, expire_on_commit=False)` connected to real PostgreSQL.
  - Assert distinct backend processes by querying `SELECT pg_backend_pid()` in each session (`pid1 != pid2`), ensuring tests do not accidentally share a connection.
  - Deterministically force lock contention and overlapping execution using synchronization barriers (e.g. `asyncio.Event` or transactional hooks) where session 1 acquires the lock or reads the old row and pauses at a synchronization point until session 2 reaches the critical section, before session 1 commits.
  - All waits and barrier synchronizations use bounded timeouts (`asyncio.wait_for`, timeout=5.0s) so deadlocks or broken synchronization fail immediately.
- **Rationale:** Simply running `asyncio.gather()` without barrier synchronization risks sequential execution where session 1 completes and commits before session 2 begins, allowing tests with defective locking to pass erroneously. Deterministic orchestration guarantees the contested code path is truly exercised.
- **Alternatives considered:** Relying on uncoordinated `asyncio.gather()` with `asyncio.sleep()`. Rejected as flaky and non-deterministic on variable CI runners.

### Decision 2: Atomic claim and idempotency in `ReleaseBookingUseCase`
- **Choice:** Implement an atomic claim / conditional transition on the booking row:
  - In `ReleaseBookingUseCase`, status updates must be atomic: `BookingRepository` performs the update conditionally (using a row lock via `SELECT ... FOR UPDATE` or conditional update `UPDATE bookings SET status = :new WHERE id = :id AND status IN (:eligible) RETURNING ...`).
  - **Only the winning transition emits side effects:**
    - For VM bookings: only the caller that successfully claims the transition to `RELEASING` calls `dispatcher.dispatch_teardown()`. A second caller finding the booking already in `RELEASING` (or `RELEASED`) returns the booking without dispatching another teardown task.
    - For pooled resources: only the caller that successfully claims the transition to `RELEASED` calls `promote_next_queued()`. A second caller finding the booking already in `RELEASED` returns the booking without calling `promote_next_queued()`.
  - **Handling of `FAILED` bookings:** `FAILED` bookings are included in `_RELEASABLE_STATUSES` and must dispatch teardown if they hold provisioned VM infrastructure. They are NOT treated as no-ops. Only already-settled `RELEASED` bookings (and `RELEASING` bookings under force release) are treated as safe no-ops without duplicate dispatch.
  - **Overlapping read integration test:** Add an explicit integration test where two concurrent release requests both read the child booking in its old `READY` status before either commits the release, verifying that only the winning session emits side effects.
- **Rationale:** A simple pre-check on an in-memory booking entity is vulnerable to read/commit races where both callers observe `READY`, both perform non-atomic updates, and both emit duplicate side effects (double Celery teardown dispatches or duplicate queue promotions).
- **Alternatives considered:** Locking only the parent environment row in `ReleaseEnvironmentUseCase`. Rejected because child bookings can also be released independently by owners, dispatchers, or background TTL tasks; per-booking atomic claims provide universal defense-in-depth.

### Decision 3: Deterministic invariant assertions and asynchronous teardown contract
- **Choice:** Structure test assertions to respect the asynchronous VM teardown contract and verify collective invariants across resource types:
  - **Pooled resource competition (namespaces and static VMs):**
    - Any-available: assert exactly one booking reaches `READY` with resource FK set, and exactly one reaches `QUEUED` with no resource FK set. Assert zero instances of duplicate resource allocation.
    - Specific-selection: assert exactly one booking reaches `READY`, while the other raises the respective unavailable error (`NamespaceUnavailableError` / `StaticVMUnavailableError`).
  - **Worker queue promotion (namespaces and static VMs):**
    - Assert `len(promoted) == 1` and `len(none_results) == 1`.
  - **Concurrent environment release:**
    - **Phase 1 (Immediate return):** When concurrent `ReleaseEnvironmentUseCase.execute()` calls return, assert both calls complete successfully without exception, the VM child is in `RELEASING`, `dispatcher.dispatch_teardown` was called *exactly once*, and the pooled child is in `RELEASED` (with `promote_next_queued` called once).
    - **Phase 2 (Convergence):** Simulate or execute the single dispatched teardown task and assert that the VM child settles to `RELEASED`, and the parent environment status derives `RELEASED`.
- **Rationale:** `ReleaseEnvironmentUseCase` returns immediately after dispatching asynchronous Celery teardown for VM children. Requiring VM children to be terminal immediately would break the asynchronous contract and cause false test failures.

## Risks / Trade-offs

- **[Risk]** Intermittent deadlocks if concurrent transactions lock rows in differing order.
  → **Mitigation:** Resource reservation and queue promotion queries order candidates deterministically (e.g., `order_by(model.name)` or `order_by(BookingModel.created_at)`), ensuring consistent lock acquisition ordering across transactions.
- **[Risk]** Database connection pool saturation under high test concurrency.
  → **Mitigation:** Tests open sessions for bounded durations and immediately close them upon completion using async context managers.
- **[Risk]** Lingering database state affecting subsequent tests.
  → **Mitigation:** Use strict `try...finally` blocks with explicit cleanup queries deleting created audit entries, bookings, and test namespaces/static VMs/environments.
