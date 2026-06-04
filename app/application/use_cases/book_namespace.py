from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import NamespaceUnavailableError
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository


class BookNamespaceUseCase:
    """Reserve a namespace from the pool — synchronous, no provisioning.

    Two modes: reserve a *specific* namespace by id, or auto-assign *any* free one;
    when the pool is empty, the any-available path enqueues (FIFO).
    """

    def __init__(self, repo: BookingRepository, namespace_repo: NamespaceRepository) -> None:
        self._repo = repo
        self._namespace_repo = namespace_repo

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
        namespace_id: UUID | None = None,
    ) -> Booking:
        uid = user_id or settings.DEV_USER_ID
        now = datetime.now(timezone.utc)

        if namespace_id is not None:
            # Pick-specific — lock the chosen row FOR UPDATE, reject if gone/inactive/taken.
            ns = await self._namespace_repo.lock_for_allocation(session, namespace_id)
            if ns is None or not ns.is_active:
                raise NamespaceUnavailableError("Namespace is not available")
            if await self._namespace_repo.is_held(session, namespace_id):
                raise NamespaceUnavailableError(f"Namespace '{ns.name}' is already booked")
        else:
            # Any-available — FOR UPDATE SKIP LOCKED so concurrent bookers take different ones.
            ns = await self._namespace_repo.lock_next_available(session)
            if ns is None:
                # Pool exhausted — enqueue (FIFO). Promoted to READY when one frees.
                return await self._enqueue(session, uid, ttl_minutes, now)

        if ttl_minutes == 0:
            expires_at = PERMANENT_EXPIRES_AT
        else:
            expires_at = now + timedelta(minutes=ttl_minutes)

        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.READY,  # nothing to provision — ready immediately
            resource_type=ResourceType.NAMESPACE,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            created_at=now,
            namespace_id=ns.id,
        )
        created = await self._repo.create(session, booking)  # commit releases the FOR UPDATE lock

        # The create() round-trip doesn't join the namespace; attach display fields directly.
        created.namespace_name = ns.name
        created.cluster_name = ns.cluster_name
        created.api_url = ns.api_url
        return created

    async def _enqueue(self, session, uid: str, ttl_minutes: int, now: datetime) -> Booking:
        """No free namespace — create a QUEUED booking (no resource yet, TTL starts on promotion)."""
        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.QUEUED,
            resource_type=ResourceType.NAMESPACE,
            ttl_minutes=ttl_minutes,
            expires_at=now,  # placeholder until promotion; enforce_ttl ignores QUEUED
            created_at=now,
        )
        return await self._repo.create(session, booking)
