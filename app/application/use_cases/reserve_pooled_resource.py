from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import BookingRepositoryPort, PooledResourceRepositoryPort
from app.config import settings
from app.domain.entities import Booking
from app.domain.lease import Lease
from app.domain.enums import BookingStatus, ResourceType


@dataclass(frozen=True)
class PooledResourceConfig:
    """Everything that differs between the pooled resource types (static VM vs namespace)."""
    resource_type: ResourceType
    unavailable_exc: type[Exception]
    label: str                                   # human label for error messages
    fk_field: str                                # Booking FK column for this resource
    attach_display: Callable[[Booking, object], None]  # copy display fields off the resource


class ReservePooledResourceUseCase:
    """Reserve a pooled resource from an admin-managed pool — synchronous, no provisioning.

    Two modes: reserve a *specific* resource by id, or auto-assign *any* free one; when the pool
    is empty, the any-available path enqueues (FIFO). Shared by static VMs and namespaces — the
    per-type differences live in ``PooledResourceConfig``.
    """

    def __init__(self, repo: BookingRepositoryPort, pool_repo: PooledResourceRepositoryPort,
                 config: PooledResourceConfig) -> None:
        self._repo = repo
        self._pool_repo = pool_repo
        self._cfg = config

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
        resource_id: UUID | None = None,
        environment_id: UUID | None = None,
        environment_label: str | None = None,
        created_by: str | None = None,
    ) -> Booking:
        cfg = self._cfg
        uid = user_id or settings.DEV_USER_ID
        now = datetime.now(timezone.utc)

        if resource_id is not None:
            # Pick-specific — lock the chosen row FOR UPDATE, reject if gone/inactive/taken.
            resource = await self._pool_repo.lock_for_allocation(session, resource_id)
            if resource is None or not resource.is_active:
                raise cfg.unavailable_exc(f"{cfg.label} is not available")
            if await self._pool_repo.is_held(session, resource_id):
                raise cfg.unavailable_exc(f"{cfg.label} '{resource.name}' is already booked")
        else:
            # Any-available — FOR UPDATE SKIP LOCKED so concurrent bookers take different ones.
            resource = await self._pool_repo.lock_next_available(session)
            if resource is None:
                # Pool exhausted — enqueue (FIFO). Promoted to READY when one frees.
                return await self._enqueue(session, uid, ttl_minutes, now, environment_id,
                                           environment_label, created_by)

        lease = Lease.starting_now(ttl_minutes, now=now)

        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.READY,  # nothing to provision — ready immediately
            resource_type=cfg.resource_type,
            ttl_minutes=ttl_minutes,
            expires_at=lease.expires_at,
            created_at=now,
            environment_id=environment_id,
            environment_label=environment_label,
            created_by=created_by,
            **{cfg.fk_field: resource.id},
        )
        created = await self._repo.create(session, booking)  # commit releases the row lock

        # The create() round-trip doesn't join the pooled resource; attach display fields directly.
        cfg.attach_display(created, resource)
        return created

    async def _enqueue(self, session, uid: str, ttl_minutes: int, now: datetime,
                       environment_id: UUID | None = None, environment_label: str | None = None,
                       created_by: str | None = None) -> Booking:
        """No free resource — create a QUEUED booking (no resource yet, TTL starts on promotion)."""
        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.QUEUED,
            resource_type=self._cfg.resource_type,
            ttl_minutes=ttl_minutes,
            # Far-future placeholder until promotion; enforce_ttl ignores QUEUED by status anyway.
            expires_at=Lease.pending(ttl_minutes).expires_at,
            created_at=now,
            environment_id=environment_id,
            environment_label=environment_label,
            created_by=created_by,
        )
        return await self._repo.create(session, booking)
