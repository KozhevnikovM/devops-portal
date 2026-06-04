# Refactor: inject a task-dispatch Protocol (#151)

**Type: Refactor (no behaviour change)** · Source: CQ#10 · Phase 4, item #15

## Motivation

The application layer (`CreateBookingUseCase`) imported the concrete Celery task
`app.tasks.provision.provision_vm_task` directly, and the bookings route lazy-imported
`teardown_vm_task` inside `release_booking` ("avoid circular import"). Both break the one-way
dependency rule: inner/application code should not depend on the outer infrastructure/Celery layer.

## Change

- Define a `TaskDispatcher` **Protocol** in the application layer (`app/application/ports.py`) with
  `dispatch_provision(...)` and `dispatch_teardown(...)`.
- Implement `CeleryTaskDispatcher` in the infrastructure layer
  (`app/infrastructure/celery_dispatcher.py`); it lazy-imports the concrete tasks and calls
  `.delay(...)`.
- `CreateBookingUseCase` takes an optional `dispatcher: TaskDispatcher`; it no longer imports the
  concrete task at module load. The bookings route constructs a single `CeleryTaskDispatcher`,
  injects it into the use case, and uses it for teardown — removing the in-function lazy import.

No behaviour change: provisioning and teardown are still dispatched via Celery `.delay`.

## Test

The existing create/release/force-delete tests still assert that the Celery tasks' `.delay` is
invoked (now reached through the dispatcher's lazy import of the concrete task); behaviour is
unchanged.

## Docs

Internal refactor; no user-facing API change, no docs update.
