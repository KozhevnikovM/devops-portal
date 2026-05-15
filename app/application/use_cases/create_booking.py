from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.tasks.provision import provision_vm_task


class CreateBookingUseCase:
    def __init__(
        self,
        repo: BookingRepository,
        image_repo: ImageRepository,
        hw_config_repo: HWConfigRepository,
    ) -> None:
        self._repo = repo
        self._image_repo = image_repo
        self._hw_config_repo = hw_config_repo

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        image_id: UUID,
        hw_config_id: UUID,
    ) -> Booking:
        image = await self._image_repo.get(session, image_id)
        hw = await self._hw_config_repo.get(session, hw_config_id)

        now = datetime.now(timezone.utc)
        if ttl_minutes == 0:
            expires_at = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        else:
            expires_at = now + timedelta(minutes=ttl_minutes)
        booking = Booking(
            id=uuid4(),
            user_id=settings.DEV_USER_ID,
            status=BookingStatus.PENDING,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            created_at=now,
            image_id=image.id,
            image_name=image.name,
            hw_config_id=hw.id,
            hw_config_name=hw.name,
        )
        booking = await self._repo.create(session, booking)
        provision_vm_task.delay(str(booking.id), str(image.id), str(hw.id))
        return booking
