from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import HWConfig
from app.infrastructure.database.models import HWConfigModel


def _to_entity(m: HWConfigModel) -> HWConfig:
    return HWConfig(
        id=m.id,
        name=m.name,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        disk_mb=m.disk_mb,
        drive_type=m.drive_type,
        is_active=m.is_active,
        created_at=m.created_at,
    )


class HWConfigRepository:
    async def list_all(self, session: AsyncSession) -> list[HWConfig]:
        result = await session.execute(
            select(HWConfigModel).order_by(HWConfigModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[HWConfig]:
        result = await session.execute(
            select(HWConfigModel)
            .where(HWConfigModel.is_active.is_(True))
            .order_by(HWConfigModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get(self, session: AsyncSession, hw_config_id: UUID) -> HWConfig:
        model = await session.get(HWConfigModel, hw_config_id)
        if model is None or not model.is_active:
            raise ValueError(f"Hardware config {hw_config_id} not found or inactive")
        return _to_entity(model)

    async def create(
        self,
        session: AsyncSession,
        name: str,
        cpus: int,
        memory_mb: int,
        disk_mb: int = 0,
        drive_type: str = "HDD",
    ) -> HWConfig:
        model = HWConfigModel(
            id=uuid4(), name=name, cpus=cpus, memory_mb=memory_mb,
            disk_mb=disk_mb, drive_type=drive_type,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def update(self, session: AsyncSession, hw_config_id: UUID, fields: dict) -> HWConfig:
        model = await session.get(HWConfigModel, hw_config_id)
        if model is None:
            raise ValueError(f"Hardware config {hw_config_id} not found")
        for key, value in fields.items():
            setattr(model, key, value)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, hw_config_id: UUID) -> None:
        model = await session.get(HWConfigModel, hw_config_id)
        if model is None:
            raise ValueError(f"Hardware config {hw_config_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, hw_config_id: UUID) -> None:
        model = await session.get(HWConfigModel, hw_config_id)
        if model is None:
            raise ValueError(f"Hardware config {hw_config_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, hw_config_id: UUID) -> None:
        model = await session.get(HWConfigModel, hw_config_id)
        if model is None:
            raise ValueError(f"Hardware config {hw_config_id} not found")
        await session.delete(model)
        await session.commit()

    def sync_get(self, session: Session, hw_config_id: UUID) -> HWConfig:
        model = session.get(HWConfigModel, hw_config_id)
        if model is None:
            raise ValueError(f"Hardware config {hw_config_id} not found")
        return _to_entity(model)
