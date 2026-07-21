from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import (
    BookingRepositoryPort, HWConfigRepositoryPort, ImageRepositoryPort, QuotaRepositoryPort,
    TaskDispatcher,
)
from app.domain.entities import Booking
from app.domain.lease import Lease
from app.domain.enums import BookingStatus, DriveType
from app.domain.exceptions import QuotaExceededError
from app.domain.resource_details import ResourceFootprint


class CreateBookingUseCase:
    def __init__(
        self,
        repo: BookingRepositoryPort,
        image_repo: ImageRepositoryPort,
        hw_config_repo: HWConfigRepositoryPort,
        quota_repo: QuotaRepositoryPort,
        dispatcher: TaskDispatcher,
    ) -> None:
        self._repo = repo
        self._image_repo = image_repo
        self._hw_config_repo = hw_config_repo
        self._quota_repo = quota_repo
        self._dispatcher = dispatcher

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        image_id: UUID,
        hw_config_id: UUID,
        user_id: str,
        startup_script: str | None = None,
        config_roles: list | None = None,
        extra_vars: dict | None = None,
        label: str | None = None,
        environment_id: UUID | None = None,
        environment_label: str | None = None,
        created_by: str | None = None,
        dispatch: bool = True,
    ) -> Booking:
        image = await self._image_repo.get(session, image_id)
        hw = await self._hw_config_repo.get(session, hw_config_id)

        uid = user_id

        # Quota check — inside the same transaction as the booking insert.
        # Take the quota-row lock *before* counting usage, so a concurrent booking for the
        # same user blocks here until we commit and then counts our booking (#142).
        limits = await self._quota_repo.get_limits_for_update(session, uid)
        used   = await self._quota_repo.count_active_resources(session, uid)

        new_cpus      = hw.cpus
        new_memory_gb = ResourceFootprint.mb_to_gb(hw.memory_mb)
        new_disk_gb   = ResourceFootprint.mb_to_gb(hw.disk_mb)

        # The config's disk counts toward the quota of its own drive type (SSD or HDD).
        is_ssd     = hw.drive_type == DriveType.SSD.value
        disk_label = "SSD" if is_ssd else "HDD"
        used_key   = "ssd_gb"     if is_ssd else "hdd_gb"
        limit_key  = "max_ssd_gb" if is_ssd else "max_hdd_gb"

        violations = []
        if used["cpus"]      + new_cpus      > limits["max_cpus"]:
            violations.append(f"CPU ({used['cpus'] + new_cpus}/{limits['max_cpus']} cores)")
        if used["memory_gb"] + new_memory_gb > limits["max_memory_gb"]:
            violations.append(f"memory ({used['memory_gb'] + new_memory_gb}/{limits['max_memory_gb']} GB)")
        if used[used_key]    + new_disk_gb   > limits[limit_key]:
            violations.append(f"{disk_label} disk ({used[used_key] + new_disk_gb}/{limits[limit_key]} GB)")

        if violations:
            raise QuotaExceededError("Quota exceeded: " + ", ".join(violations))

        now = datetime.now(timezone.utc)
        lease = Lease.starting_now(ttl_minutes, now=now)

        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.PENDING,
            ttl_minutes=ttl_minutes,
            expires_at=lease.expires_at,
            created_at=now,
            image_id=image.id,
            image_name=image.name,
            hw_config_id=hw.id,
            hw_config_name=hw.name,
            cpus=hw.cpus,
            memory_mb=hw.memory_mb,
            disk_mb=hw.disk_mb,
            drive_type=hw.drive_type,
            startup_script=startup_script or None,
            config_roles=config_roles or [],
            extra_vars=extra_vars or {},
            label=label,
            environment_id=environment_id,
            environment_label=environment_label,
            created_by=created_by,
        )
        booking = await self._repo.create(session, booking)
        # Ordering an environment defers dispatch until all children are created (clean rollback).
        if dispatch:
            self._dispatcher.dispatch_provision(str(booking.id), str(image.id), str(hw.id))
        return booking
