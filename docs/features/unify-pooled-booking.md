# Refactor: unify the pooled-booking use cases (#150)

**Type: Refactor (no behaviour change)** · Source: CQ#11/#12 · Phase 4, item #14

## Motivation

`BookNamespaceUseCase` and `ReserveStaticVMUseCase` are ~90% identical: both do pick-specific
(lock `FOR UPDATE`, reject if gone/inactive/held) or any-available (`FOR UPDATE SKIP LOCKED`, else
enqueue FIFO), create a `READY` booking, and attach display fields. The only differences are the
pool repository, the resource type, the unavailable-exception type, the error label, the booking FK
field, and which display fields get copied off the resource.

## Change

Extract a single parameterized `ReservePooledResourceUseCase`
(`app/application/use_cases/reserve_pooled_resource.py`) that holds the shared flow, configured by a
small `_PooledResourceConfig` (resource type, exception, label, FK field, display-attach callback).

`BookNamespaceUseCase` and `ReserveStaticVMUseCase` are kept as **thin adapters** over the base so
that **all existing call sites and signatures are unchanged** (the routes still construct
`BookNamespaceUseCase(repo, namespace_repo)` and call `.execute(..., namespace_id=...)`; likewise
for static VMs). This contains the change to the use-case layer.

**Scope note.** The plan also mentioned reducing the `Booking` entity's denormalized display fields
via a separate read/view model. That touches every template and JSON serializer and is deferred —
this refactor delivers the use-case dedup (the bulk of the duplication) without destabilizing the
presentation layer.

## Test

The existing pooled-booking tests (`test_namespace_booking.py`, `test_static_vm_booking.py`,
`test_booking_queue.py`) exercise both adapters unchanged and stay green — proving behaviour is
preserved (pick-specific, any-available, enqueue, display fields, errors).

## Docs

Internal refactor; no user-facing API change, no docs update.
