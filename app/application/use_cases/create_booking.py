from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.exceptions import QuotaExceededError
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository
from app.tasks.provision import provision_vm_task


class CreateBookingUseCase:
    def __init__(
        self,
        repo: BookingRepository,
        image_repo: ImageRepository,
        hw_config_repo: HWConfigRepository,
        quota_repo: QuotaRepository | None = None,
    ) -> None:
        self._repo = repo
        self._image_repo = image_repo
        self._hw_config_repo = hw_config_repo
        self._quota_repo = quota_repo or QuotaRepository()

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        image_id: UUID,
        hw_config_id: UUID,
        user_id: str | None = None,
    ) -> Booking:
        image = await self._image_repo.get(session, image_id)
        hw = await self._hw_config_repo.get(session, hw_config_id)

        uid = user_id or settings.DEV_USER_ID

        # Quota check — inside the same transaction as the booking insert.
        # Take the quota-row lock *before* counting usage, so a concurrent booking for the
        # same user blocks here until we commit and then counts our booking (#142).
        limits = await self._quota_repo.get_limits_for_update(session, uid)
        used   = await self._quota_repo.count_active_resources(session, uid)

        new_cpus      = hw.cpus
        new_memory_gb = hw.memory_mb // 1024  # floor: matches ceiling on the used-side at the boundary
        new_hdd_gb    = hw.hdd_mb    // 1024

        violations = []
        if used["cpus"]      + new_cpus      > limits["max_cpus"]:
            violations.append(f"CPU ({used['cpus'] + new_cpus}/{limits['max_cpus']} cores)")
        if used["memory_gb"] + new_memory_gb > limits["max_memory_gb"]:
            violations.append(f"memory ({used['memory_gb'] + new_memory_gb}/{limits['max_memory_gb']} GB)")
        if used["hdd_gb"]    + new_hdd_gb    > limits["max_hdd_gb"]:
            violations.append(f"HDD ({used['hdd_gb'] + new_hdd_gb}/{limits['max_hdd_gb']} GB)")

        if violations:
            raise QuotaExceededError("Quota exceeded: " + ", ".join(violations))

        now = datetime.now(timezone.utc)
        if ttl_minutes == 0:
            expires_at = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        else:
            expires_at = now + timedelta(minutes=ttl_minutes)

        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.PENDING,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            created_at=now,
            image_id=image.id,
            image_name=image.name,
            hw_config_id=hw.id,
            hw_config_name=hw.name,
            cpus=hw.cpus,
            memory_mb=hw.memory_mb,
            hdd_mb=hw.hdd_mb,
        )
        booking = await self._repo.create(session, booking)
        provision_vm_task.delay(str(booking.id), str(image.id), str(hw.id))
        return booking
