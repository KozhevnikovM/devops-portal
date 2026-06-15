# Refactor: repository interfaces (close the application→infrastructure dependency leak)

## Goal

Make the **application layer depend on abstractions, not concrete infrastructure**, so the codebase
actually obeys the rule stated in `CLAUDE.md` and `docs/architecure.md`:

> inner layers have no imports from outer layers (`domain → application → infrastructure → presentation`)

Today every use case imports concrete SQLAlchemy repositories, e.g.
[create_booking.py:12-15](../../app/application/use_cases/create_booking.py#L12-L15):

```python
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository
```

The `TaskDispatcher` port in [ports.py](../../app/application/ports.py) already shows the target
pattern. This refactor extends that pattern to repositories.

## Root cause

`application/ports.py` defines exactly one port (`TaskDispatcher`). Repositories were never given a
port, so use cases reference concrete classes — both in imports and in type hints — which points an
inner-layer arrow at the outer layer.

## What changes

### 1. New port module(s) in the application layer

Add `Protocol` definitions describing **only the methods the use cases actually call**. Keep them in
`app/application/ports.py` (or split into `app/application/ports/` if it grows). Protocols are
structural, so the existing concrete repositories satisfy them **without inheriting anything** — no
change to the infrastructure classes is required.

Scope is deliberately the *async* methods used by use cases (the `sync_*` methods are called only by
Celery tasks in the infrastructure/presentation-adjacent layer and stay outside these ports for now).

Ports to add, derived from current call sites:

```python
# app/application/ports.py
from typing import Protocol
from uuid import UUID
from datetime import datetime
from app.domain.entities import Booking, VMImage, HWConfig, Namespace, StaticVM
# NOTE: AsyncSession is a SQLAlchemy type. See "Open question: the session parameter" below.

class BookingRepositoryPort(Protocol):
    async def create(self, session, booking: Booking) -> Booking: ...
    async def get(self, session, booking_id: UUID) -> Booking: ...
    async def update_status(self, session, booking_id: UUID, status, *,
                            vm_ip: str | None = None, vm_password: str | None = None,
                            actor_id: str = "system") -> None: ...
    async def extend(self, session, booking_id: UUID, extend_minutes: int, actor_id: str) -> None: ...
    async def promote_next_queued(self, session, resource_type: str) -> Booking | None: ...
    async def queue_position(self, session, resource_type: str, created_at: datetime) -> int: ...

class ImageRepositoryPort(Protocol):
    async def get(self, session, image_id: UUID) -> VMImage: ...
    async def get_by_name(self, session, name: str) -> VMImage | None: ...

class HWConfigRepositoryPort(Protocol):
    async def get(self, session, hw_config_id: UUID) -> HWConfig: ...
    async def get_by_name(self, session, name: str) -> HWConfig | None: ...

class QuotaRepositoryPort(Protocol):
    async def get_limits_for_update(self, session, user_id: str) -> dict: ...
    async def count_active_resources(self, session, user_id: str) -> dict: ...

class PooledResourceRepositoryPort(Protocol):
    """Satisfied by both NamespaceRepository and StaticVMRepository."""
    async def lock_for_allocation(self, session, resource_id: UUID): ...
    async def lock_next_available(self, session): ...
    async def is_held(self, session, resource_id: UUID) -> bool: ...
```

Add `NamespaceRepositoryPort` / `StaticVMRepositoryPort` and `EnvironmentRepositoryPort` similarly,
covering only the methods their use cases call (`book_namespace` adds `get_by_name_and_cluster`;
`order_environment` uses `env_repo.create / start_lease_if_ready / get / delete` plus the blueprint,
role, and static-vm `get_by_name` lookups).

### 2. Use cases type against ports, not concrete classes

In each use case, replace the concrete imports + type hints with the port types:

```python
# create_booking.py — before
from app.infrastructure.repositories.booking_repo import BookingRepository
def __init__(self, repo: BookingRepository, ...): ...

# after
from app.application.ports import BookingRepositoryPort, ImageRepositoryPort, ...
def __init__(self, repo: BookingRepositoryPort, ...): ...
```

The lazy `QuotaRepository()` default inside `CreateBookingUseCase.__init__` must move out — a default
that constructs a concrete infra class re-introduces the import. Make `quota_repo` a required
constructor arg supplied by the composition root (see §4). Same treatment for the lazy
`CeleryTaskDispatcher` import in `_dispatch()` — that one is acceptable to keep as-is short-term
since it is already isolated behind a method, but ideally it too is injected.

`OrderEnvironmentUseCase` ([order_environment.py:20-24](../../app/application/use_cases/order_environment.py#L20-L24))
currently takes ten **untyped** constructor params — this refactor is the moment to give each a port
type, which is a strict readability win on its own.

### 3. Infrastructure stays as-is (structural typing)

Because `Protocol` is structural, `BookingRepository` already *is* a `BookingRepositoryPort`. No base
class, no `implements`, no edits to `app/infrastructure/repositories/*`. Optionally add a
`# satisfies BookingRepositoryPort` comment for readers, but nothing functional changes.

### 4. Composition root (small, enables the above)

The lazy concrete defaults exist today only because there is no single place to assemble the object
graph; each route module instead news-up its own repositories
([bookings.py:31-41](../../app/presentation/routes/bookings.py#L31-L41)). Introduce one assembly
point — e.g. `app/presentation/deps.py` (or `app/composition.py`) — that constructs the concrete
repositories and use cases once and exposes them (FastAPI `Depends` providers for routes; plain
module singletons for the Celery side). This is what lets §2 drop the lazy defaults.

This step can be deferred to a follow-up PR if we want to keep the first PR purely additive; in that
case keep the existing lazy defaults temporarily and only switch type hints.

## Open question: the `session` parameter

Every repository method takes a SQLAlchemy `AsyncSession`. Strictly, that type leaking into the port
signatures is still an infrastructure dependency in the application layer. Three options, in
increasing purity / cost:

1. **Pragmatic (recommended for this PR):** type the param as `session: AsyncSession`, accept the
   one remaining SQLAlchemy import in `ports.py`. 90% of the benefit, near-zero risk. The
   "use cases receive a session as a parameter" contract is already established in `CLAUDE.md`.
2. Type it as `session: Any` / a thin `Session` Protocol — removes the literal import, keeps the
   shape.
3. Full Unit-of-Work pattern — repositories hold the session, use cases depend on a `UnitOfWork`
   port. Larger change; out of scope here, worth a separate proposal.

Recommendation: **option 1 now**, revisit UoW later if we ever need a non-SQLAlchemy backend.

## Non-goals

- No behavior change. No SQL change. No new domain logic. Status-invariant enforcement and the
  `Lease` value object are tracked separately (see `lease-value-object.md`).
- Not introducing a DI framework — plain constructor injection + one composition module.

## Testing

- Existing test suite must stay green unchanged — this is a pure structural refactor.
- Add a lightweight test that the concrete repositories satisfy their ports, e.g. an
  `isinstance(BookingRepository(), BookingRepositoryPort)` check using
  `@runtime_checkable` Protocols, so a future signature drift fails fast.
- `mypy`/`pyright` (if/when added) would catch port conformance statically; note as a follow-up.

## Suggested PR sequence

1. **PR 1 (additive):** add `ports.py` Protocols + `@runtime_checkable` conformance test. No call-site
   changes. Zero risk.
2. **PR 2:** flip use-case type hints to ports; add composition root; drop lazy concrete defaults.
3. **PR 3 (optional):** route/task wiring through the composition root, removing per-module
   repository singletons.

## Rollback

Each PR is independently revertible; PR 1 is inert until PR 2 consumes it.
