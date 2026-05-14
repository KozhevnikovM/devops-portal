from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.template_repo import TemplateRepository
from app.tasks.provision import provision_vm_task


class CreateBookingUseCase:
    def __init__(self, repo: BookingRepository, template_repo: TemplateRepository) -> None:
        self._repo = repo
        self._template_repo = template_repo

    async def execute(self, session: AsyncSession, ttl_hours: int, template_id: UUID) -> Booking:
        template = await self._template_repo.get(session, template_id)

        now = datetime.now(timezone.utc)
        booking = Booking(
            id=uuid4(),
            user_id=settings.DEV_USER_ID,
            status=BookingStatus.PENDING,
            ttl_hours=ttl_hours,
            expires_at=now + timedelta(hours=ttl_hours),
            created_at=now,
            template_id=template.id,
            template_name=template.name,
        )
        booking = await self._repo.create(session, booking)
        provision_vm_task.delay(str(booking.id), str(template.id))
        return booking
