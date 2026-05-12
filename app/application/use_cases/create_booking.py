from datetime import datetime, timezone, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.tasks.provision import provision_vm_task


class CreateBookingUseCase:
    def __init__(self, repo: BookingRepository) -> None:
        self._repo = repo

    async def execute(self, session: AsyncSession, ttl_hours: int) -> Booking:
        now = datetime.now(timezone.utc)
        booking = Booking(
            id=uuid4(),
            user_id=settings.DEV_USER_ID,
            status=BookingStatus.PENDING,
            ttl_hours=ttl_hours,
            expires_at=now + timedelta(hours=ttl_hours),
            created_at=now,
        )
        booking = await self._repo.create(session, booking)
        provision_vm_task.delay(str(booking.id))
        return booking
