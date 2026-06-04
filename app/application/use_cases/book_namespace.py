from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking
from app.domain.enums import ResourceType
from app.domain.exceptions import NamespaceUnavailableError
from app.application.use_cases.reserve_pooled_resource import (
    PooledResourceConfig, ReservePooledResourceUseCase,
)
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository


def _attach_namespace(booking: Booking, ns) -> None:
    booking.namespace_name = ns.name
    booking.cluster_name = ns.cluster_name
    booking.api_url = ns.api_url


_NAMESPACE_CONFIG = PooledResourceConfig(
    resource_type=ResourceType.NAMESPACE,
    unavailable_exc=NamespaceUnavailableError,
    label="Namespace",
    fk_field="namespace_id",
    attach_display=_attach_namespace,
)


class BookNamespaceUseCase(ReservePooledResourceUseCase):
    """Reserve a namespace from the pool (specific or any-available, else enqueue)."""

    def __init__(self, repo: BookingRepository, namespace_repo: NamespaceRepository) -> None:
        super().__init__(repo, namespace_repo, _NAMESPACE_CONFIG)

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
        namespace_id: UUID | None = None,
    ) -> Booking:
        return await super().execute(session, ttl_minutes, user_id=user_id, resource_id=namespace_id)
