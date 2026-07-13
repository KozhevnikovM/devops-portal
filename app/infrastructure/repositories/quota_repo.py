import math
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Quota
from app.domain.enums import BookingStatus, DriveType
from app.infrastructure.database.models import BookingModel, QuotaModel

_ACTIVE_STATUSES = [
    BookingStatus.PENDING.value,
    BookingStatus.PROVISIONING.value,
    BookingStatus.CONFIGURING.value,
    BookingStatus.RETRY.value,
    BookingStatus.READY.value,
    BookingStatus.RELEASING.value,
]


def _to_entity(m: QuotaModel) -> Quota:
    return Quota(
        id=m.id,
        user_id=m.user_id,
        max_cpus=m.max_cpus,
        max_memory_gb=m.max_memory_gb,
        max_ssd_gb=m.max_ssd_gb,
        max_hdd_gb=m.max_hdd_gb,
        created_at=m.created_at,
    )


def _default_limits() -> dict:
    return {
        "max_cpus":      settings.DEFAULT_QUOTA_CPUS,
        "max_memory_gb": settings.DEFAULT_QUOTA_MEMORY_GB,
        "max_ssd_gb":    settings.DEFAULT_QUOTA_SSD_GB,
        "max_hdd_gb":    settings.DEFAULT_QUOTA_HDD_GB,
    }


def _model_to_limits(m: QuotaModel) -> dict:
    return {
        "max_cpus":      m.max_cpus,
        "max_memory_gb": m.max_memory_gb,
        "max_ssd_gb":    m.max_ssd_gb,
        "max_hdd_gb":    m.max_hdd_gb,
    }


class QuotaRepository:
    async def count_active_resources(self, session: AsyncSession, user_id: str) -> dict:
        result = await session.execute(
            select(
                func.coalesce(func.sum(BookingModel.cpus),      0).label("cpus"),
                func.coalesce(func.sum(BookingModel.memory_mb), 0).label("memory_mb"),
            ).where(
                BookingModel.user_id == user_id,
                BookingModel.status.in_(_ACTIVE_STATUSES),
            )
        )
        row = result.one()

        # Disk is summed per drive type so it counts toward the matching quota (SSD vs HDD).
        disk_result = await session.execute(
            select(
                BookingModel.drive_type,
                func.coalesce(func.sum(BookingModel.disk_mb), 0).label("disk_mb"),
            ).where(
                BookingModel.user_id == user_id,
                BookingModel.status.in_(_ACTIVE_STATUSES),
            ).group_by(BookingModel.drive_type)
        )
        disk_mb_by_type = {dt: int(disk_mb) for dt, disk_mb in disk_result.all()}

        return {
            "cpus":      int(row.cpus),
            "memory_gb": math.ceil(int(row.memory_mb) / 1024),
            "ssd_gb":    math.ceil(disk_mb_by_type.get(DriveType.SSD.value, 0) / 1024),
            "hdd_gb":    math.ceil(disk_mb_by_type.get(DriveType.HDD.value, 0) / 1024),
        }

    async def get_limits(self, session: AsyncSession, user_id: str) -> dict:
        result = await session.execute(
            select(QuotaModel).where(QuotaModel.user_id == UUID(user_id))
        )
        model = result.scalar_one_or_none()
        return _model_to_limits(model) if model else _default_limits()

    async def get_limits_for_update(self, session: AsyncSession, user_id: str) -> dict:
        # Lazy-seed the quota row from defaults so it always exists. Otherwise a
        # default-quota user (no row) would lock nothing and the FOR UPDATE below would
        # be a no-op, letting concurrent bookings race past the limit (#142).
        await session.execute(
            pg_insert(QuotaModel)
            .values(id=uuid4(), user_id=UUID(user_id), **_default_limits())
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        result = await session.execute(
            select(QuotaModel)
            .where(QuotaModel.user_id == UUID(user_id))
            .with_for_update()
        )
        return _model_to_limits(result.scalar_one())

    async def set(
        self,
        session: AsyncSession,
        user_id: UUID,
        max_cpus: int,
        max_memory_gb: int,
        max_ssd_gb: int,
        max_hdd_gb: int,
    ) -> Quota:
        stmt = (
            pg_insert(QuotaModel)
            .values(
                id=uuid4(),
                user_id=user_id,
                max_cpus=max_cpus,
                max_memory_gb=max_memory_gb,
                max_ssd_gb=max_ssd_gb,
                max_hdd_gb=max_hdd_gb,
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "max_cpus":      max_cpus,
                    "max_memory_gb": max_memory_gb,
                    "max_ssd_gb":    max_ssd_gb,
                    "max_hdd_gb":    max_hdd_gb,
                },
            )
            .returning(QuotaModel)
        )
        result = await session.execute(stmt)
        await session.commit()
        return _to_entity(result.scalar_one())
