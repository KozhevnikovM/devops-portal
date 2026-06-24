"""Application-layer ports (interfaces the use cases depend on).

Inner layers must not import outer ones (`domain → application → infrastructure → presentation`).
These `Protocol`s describe the repository/dispatcher methods the use cases call, so use cases can
type against an abstraction instead of a concrete `app.infrastructure.repositories.*` class.

Protocols are **structural**: the existing concrete repositories already satisfy these without
inheriting anything (verified by `tests/test_repository_ports.py`). Only the *async* methods the use
cases call are modelled here; the `sync_*` methods (Celery side) stay outside these ports for now.

The one pragmatic concession (see `docs/refactor/repository-interfaces.md`): the SQLAlchemy
`AsyncSession` still appears in signatures — the established "use cases receive a session parameter"
contract — rather than introducing a Unit-of-Work abstraction.
"""
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    Booking, Environment, EnvironmentBlueprint, HWConfig, Namespace, Role, StaticVM, VMImage,
)
from app.domain.enums import BookingStatus


class TaskDispatcher(Protocol):
    """Port for dispatching background jobs, so the application layer never imports the
    concrete Celery tasks (preserves the one-way dependency rule)."""

    def dispatch_provision(self, booking_id: str, image_id: str, hw_config_id: str) -> None: ...

    def dispatch_teardown(self, booking_id: str) -> None: ...


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


@runtime_checkable
class BlueprintRepositoryPort(Protocol):
    async def get_by_name(self, session: AsyncSession, name: str) -> EnvironmentBlueprint | None: ...


@runtime_checkable
class RoleRepositoryPort(Protocol):
    async def get_by_name(self, session: AsyncSession, name: str) -> Role | None: ...
