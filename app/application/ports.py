"""Application-layer ports (interfaces the use cases depend on).

Inner layers must not import outer ones (`domain → application → infrastructure → presentation`).
These `Protocol`s describe the repository/dispatcher methods the use cases call, so use cases can
type against an abstraction instead of a concrete `app.infrastructure.repositories.*` class.

Protocols are **structural**: the existing concrete repositories already satisfy these without
inheriting anything (verified by `tests/test_repository_ports.py`). Most ports here model only the
*async* methods the use cases (FastAPI side) call; `SyncBookingRepositoryPort` is the one exception,
modelling the `sync_*` methods the Celery worker (`app/tasks/provision.py`, `app/tasks/teardown.py`)
calls on the sync side of the same repository.

The one pragmatic concession (see `docs/refactor/repository-interfaces.md`): the SQLAlchemy
`AsyncSession`/`Session` still appears in signatures — the established "use cases receive a session
parameter" contract — rather than introducing a Unit-of-Work abstraction.
"""
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import (
    Booking, Environment, EnvironmentBlueprint, HWConfig, Namespace, Role, StaticVM, VMImage,
)
from app.domain.enums import BookingStatus


class TaskDispatcher(Protocol):
    """Port for dispatching background jobs, so the application layer never imports the
    concrete Celery tasks (preserves the one-way dependency rule).

    Every method accepts an optional `request_id` (#371 F-3): the correlation id the FastAPI
    middleware bound to the request that triggered the dispatch, threaded through by the calling
    use case (same pattern as `booking_id`). `None` when there is no request in flight (e.g. the
    startup-recovery / beat-scheduled dispatches that call the concrete tasks directly rather than
    through this port). Celery task args must be JSON-serialisable, so this can't rely on the
    contextvar crossing the process boundary — it has to travel as an explicit argument."""

    def dispatch_provision(
        self, booking_id: str, image_id: str, hw_config_id: str, request_id: str | None = None,
    ) -> None: ...

    def dispatch_teardown(self, booking_id: str, request_id: str | None = None) -> None: ...

    def dispatch_teardown_force(self, booking_id: str, request_id: str | None = None) -> None: ...


@runtime_checkable
class BookingRepositoryPort(Protocol):
    async def create(self, session: AsyncSession, booking: Booking) -> Booking: ...
    async def get(self, session: AsyncSession, booking_id: UUID) -> Booking: ...
    async def update_status(
        self, session: AsyncSession, booking_id: UUID, status: BookingStatus,
        vm_ip: str | None = None, vm_password: str | None = None, actor_id: str = "system",
    ) -> None: ...
    async def extend(
        self, session: AsyncSession, booking_id: UUID, extend_minutes: int, actor_id: str,
    ) -> None: ...
    async def update_label(
        self, session: AsyncSession, booking_id: UUID, label: str | None, actor_id: str,
    ) -> None: ...
    async def promote_next_queued(self, session: AsyncSession, resource_type: str) -> Booking | None: ...
    async def queue_position(self, session: AsyncSession, resource_type: str, created_at: datetime) -> int: ...
    async def get_live_standalone_namespace_booking(
        self, session: AsyncSession, user_id: str, namespace_id: UUID,
    ) -> Booking | None: ...
    async def set_environment(
        self, session: AsyncSession, booking_id: UUID, environment_id: UUID | None,
        environment_label: str | None, ttl_minutes: int, expires_at: datetime,
    ) -> None: ...


@runtime_checkable
class SyncBookingRepositoryPort(Protocol):
    """Sync twin of `BookingRepositoryPort`, for the Celery worker path (`provision.py`,
    `teardown.py`). Models only the `sync_*` methods those two tasks actually call — other
    sync methods (e.g. `sync_list_expired`, used only by `beat_tasks.py`) aren't included."""
    def sync_get(self, session: Session, booking_id: UUID) -> Booking: ...
    def sync_update_status(
        self, session: Session, booking_id: UUID, status: BookingStatus,
        vm_ip: str | None = None, vm_password: str | None = None,
        config_failed: bool | None = None, start_lease: bool = False, actor_id: str = "system",
    ) -> None: ...
    def sync_set_status_message(
        self, session: Session, booking_id: UUID, message: str | None,
    ) -> None: ...
    def sync_record_progress(
        self, session: Session, booking_id: UUID, message: str,
    ) -> None: ...
    def sync_promote_next_queued(self, session: Session, resource_type: str) -> Booking | None: ...


@runtime_checkable
class ImageRepositoryPort(Protocol):
    async def get(self, session: AsyncSession, image_id: UUID) -> VMImage: ...
    async def get_by_name(self, session: AsyncSession, name: str) -> VMImage | None: ...


@runtime_checkable
class HWConfigRepositoryPort(Protocol):
    async def get(self, session: AsyncSession, hw_config_id: UUID) -> HWConfig: ...
    async def get_by_name(self, session: AsyncSession, name: str) -> HWConfig | None: ...


@runtime_checkable
class QuotaRepositoryPort(Protocol):
    async def get_limits_for_update(self, session: AsyncSession, user_id: str) -> dict: ...
    async def count_active_resources(self, session: AsyncSession, user_id: str) -> dict: ...


@runtime_checkable
class PooledResourceRepositoryPort(Protocol):
    """Common pool operations — satisfied by both NamespaceRepository and StaticVMRepository.

    The lock methods return the concrete pooled-resource ORM row (or None); the use case only reads
    `.id` / `.is_active` / `.name` off it, so the port keeps the return loose to avoid importing an
    infrastructure model into the application layer."""
    async def lock_for_allocation(self, session: AsyncSession, resource_id: UUID) -> object | None: ...
    async def lock_next_available(self, session: AsyncSession) -> object | None: ...
    async def is_held(self, session: AsyncSession, resource_id: UUID) -> bool: ...


@runtime_checkable
class NamespaceRepositoryPort(PooledResourceRepositoryPort, Protocol):
    async def get_by_name_and_cluster(
        self, session: AsyncSession, name: str, cluster_name: str,
    ) -> Namespace | None: ...
    async def list_held_standalone_by_user(
        self, session: AsyncSession, user_id: str,
    ) -> list[Namespace]: ...


@runtime_checkable
class StaticVMRepositoryPort(PooledResourceRepositoryPort, Protocol):
    async def get_by_name(self, session: AsyncSession, name: str) -> StaticVM | None: ...


@runtime_checkable
class EnvironmentRepositoryPort(Protocol):
    async def create(
        self, session: AsyncSession, name: str, blueprint_name: str | None,
        user_id: str, ttl_minutes: int, expires_at, created_by: str | None = None,
    ) -> Environment: ...
    async def get(self, session: AsyncSession, environment_id: UUID) -> Environment: ...
    async def delete(self, session: AsyncSession, environment_id: UUID) -> None: ...
    async def start_lease_if_ready(self, session: AsyncSession, environment_id: UUID) -> bool: ...
    async def update_name(self, session: AsyncSession, environment_id: UUID, name: str) -> None: ...


@runtime_checkable
class BlueprintRepositoryPort(Protocol):
    async def get_by_name(self, session: AsyncSession, name: str) -> EnvironmentBlueprint | None: ...


@runtime_checkable
class RoleRepositoryPort(Protocol):
    async def get_by_name(self, session: AsyncSession, name: str) -> Role | None: ...
