from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import NamespaceUnavailableError
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository


class BookNamespaceUseCase:
    """Allocate a pre-created namespace from the pool — synchronous, no provisioning."""

    def __init__(self, repo: BookingRepository, namespace_repo: NamespaceRepository) -> None:
        self._repo = repo
        self._namespace_repo = namespace_repo

    async def execute(
        self,
        session: AsyncSession,
        namespace_id: UUID,
        ttl_minutes: int,
        user_id: str | None = None,
    ) -> Booking:
        uid = user_id or settings.DEV_USER_ID

        # Lock the namespace row FOR UPDATE so two concurrent bookers can't both take it.
        ns = await self._namespace_repo.lock_for_allocation(session, namespace_id)
        if ns is None or not ns.is_active:
            raise NamespaceUnavailableError("Namespace is not available")
        if await self._namespace_repo.is_held(session, namespace_id):
            raise NamespaceUnavailableError(f"Namespace '{ns.name}' is already booked")

        now = datetime.now(timezone.utc)
        if ttl_minutes == 0:
            expires_at = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
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
