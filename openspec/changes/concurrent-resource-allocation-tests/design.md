## Context

Scarce resources in the portal comprise pooled resources (namespaces and static VMs) and multi-booking parent environments. Allocation and promotion rely on PostgreSQL row locking (`FOR UPDATE` for specific resources, `FOR UPDATE SKIP LOCKED` for any-available allocation and queue promotions). While unit tests mock repository ports, issue #452 (parent #433) requires Postgres-backed race-condition tests executing against independent database connections to verify that locking and status guards prevent duplicate ownership and double releases under real concurrency.

See `proposal.md` for background and `specs/scarce-resource-concurrency/spec.md` and `specs/environment-lifecycle/spec.md` for requirements.

## Goals / Non-Goals

**Goals:**
- Provide automated, PostgreSQL-backed race-condition integration tests covering:
  1. Two bookings concurrently requesting the last free namespace (any-available and specific-selection paths).
  2. Two workers concurrently allocating/promoting the same scarce resource.
  3. Two releases running concurrently for the same environment.
  4. Allocation racing with teardown/release of a pooled resource.
- Enforce core invariants: single active ownership of pooled resources, correct availability/queue behavior, and no double releases or lost updates.
- Ensure `ReleaseBookingUseCase` and `ReleaseEnvironmentUseCase` are idempotent when handling concurrent release attempts.

**Non-Goals:**
- Schema changes or database migrations: existing PostgreSQL constraints and row-level locking mechanisms (`FOR UPDATE`, `FOR UPDATE SKIP LOCKED`) are sufficient.
- End-to-end browser or Locust HTTP load testing (covered by `load-testing`).
- Altering the domain transition graph or status machine rules.

## Decisions

### Decision 1: Concurrency driver and session isolation
- **Choice:** Use `asyncio.gather()` executing use cases or repository operations in distinct `AsyncSession(async_engine, expire_on_commit=False)` contexts against real PostgreSQL.
- **Rationale:** This pattern matches existing integration tests (`test_quota_concurrent_writes.py`, `test_queue_promotion.py`). Each session obtains its own database connection from the engine's pool, initiating separate PostgreSQL transactions that race over row locks in the database engine.
- **Alternatives considered:** Thread-based concurrency with sync sessions. Rejected because the application use cases (`ReservePooledResourceUseCase`, `ReleaseEnvironmentUseCase`, `ReleaseBookingUseCase`) and repositories are natively async, so `asyncio.gather` with independent async sessions is the idiomatic, non-blocking approach.

### Decision 2: Idempotent release in `ReleaseBookingUseCase`
- **Choice:** Make `ReleaseBookingUseCase` safely idempotent when handling bookings that are already in `RELEASED` or `RELEASING` status:
  - If a booking is already `RELEASED`, return the booking as a no-op without re-triggering promotion or raising an `IllegalStatusTransitionError`.
  - If a booking is already `RELEASING` during a forced release (`force=True`), return without dispatching redundant teardown tasks.
- **Rationale:** In concurrent environment release scenarios, two workers or release triggers may both read the environment's child list before either commits. Without idempotency at the booking release layer, the second caller attempts an invalid status transition (e.g. `RELEASED` → `RELEASING`) or dispatches duplicate teardown tasks.
- **Alternatives considered:** Locking the `EnvironmentModel` row exclusively in `ReleaseEnvironmentUseCase`. While locking the parent environment serializes environment-level calls, child bookings may also be released via individual API calls or background TTL expiration tasks. Hardening `ReleaseBookingUseCase` directly provides defense-in-depth across all callers.

### Decision 3: Deterministic invariant assertions
- **Choice:** Assert on collective set invariants rather than execution order:
  - For two bookings competing for 1 namespace: assert `len(ready) == 1`, `len(queued_or_rejected) == 1`, and unique `namespace_id` assignment.
  - For two workers racing queue promotion: assert `len(promoted) == 1` and `len(none_results) == 1`.
  - For concurrent environment releases: assert both calls complete successfully, teardown is dispatched exactly once per VM child, and all children reach terminal states.
- **Rationale:** Under real concurrent execution, operating system scheduling determines which transaction reaches PostgreSQL first. Asserting on the aggregated invariants ensures tests are deterministic, robust, and free of timing flakiness.

## Risks / Trade-offs

- **[Risk]** Intermittent deadlocks if concurrent transactions lock rows in differing order.
  → **Mitigation:** Resource reservation and queue promotion queries order candidates deterministically (e.g., `order_by(model.name)` or `order_by(BookingModel.created_at)`), ensuring consistent lock acquisition ordering across transactions.
- **[Risk]** Database connection pool saturation under high test concurrency.
  → **Mitigation:** Tests open sessions for bounded durations and immediately close them upon completion using async context managers.
- **[Risk]** Lingering database state affecting subsequent tests.
  → **Mitigation:** Use strict `try...finally` blocks with explicit cleanup queries deleting created audit entries, bookings, and test namespaces/environments.
