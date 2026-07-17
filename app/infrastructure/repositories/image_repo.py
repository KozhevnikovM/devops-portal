from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import VMImage
from app.domain.exceptions import ImageNotFoundError
from app.infrastructure.database.models import VMImageModel


def _to_entity(m: VMImageModel) -> VMImage:
    return VMImage(
        id=m.id,
        name=m.name,
        vapp_template_id=m.vapp_template_id,
        is_active=m.is_active,
        created_at=m.created_at,
    )


class ImageRepository:
    async def list_all(self, session: AsyncSession) -> list[VMImage]:
        result = await session.execute(
            select(VMImageModel).order_by(VMImageModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[VMImage]:
        result = await session.execute(
            select(VMImageModel)
            .where(VMImageModel.is_active.is_(True))
            .order_by(VMImageModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get(self, session: AsyncSession, image_id: UUID) -> VMImage:
        model = await session.get(VMImageModel, image_id)
        if model is None or not model.is_active:
            raise ImageNotFoundError(f"VM image {image_id} not found or inactive")
        return _to_entity(model)

    async def get_by_name(self, session: AsyncSession, name: str) -> VMImage | None:
        """Resolve an *active* VM image by its (unique) name; None if no active match."""
        result = await session.execute(
            select(VMImageModel).where(
                VMImageModel.name == name,
                VMImageModel.is_active.is_(True),
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def create(self, session: AsyncSession, name: str, vapp_template_id: str) -> VMImage:
        model = VMImageModel(id=uuid4(), name=name, vapp_template_id=vapp_template_id)
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def update(self, session: AsyncSession, image_id: UUID, fields: dict) -> VMImage:
        model = await session.get(VMImageModel, image_id)
        if model is None:
            raise ImageNotFoundError(f"VM image {image_id} not found")
        for key, value in fields.items():
            setattr(model, key, value)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, image_id: UUID) -> None:
        model = await session.get(VMImageModel, image_id)
        if model is None:
            raise ImageNotFoundError(f"VM image {image_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, image_id: UUID) -> None:
        model = await session.get(VMImageModel, image_id)
        if model is None:
            raise ImageNotFoundError(f"VM image {image_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, image_id: UUID) -> None:
        model = await session.get(VMImageModel, image_id)
        if model is None:
            raise ImageNotFoundError(f"VM image {image_id} not found")
        await session.delete(model)
        await session.commit()

    def sync_get(self, session: Session, image_id: UUID) -> VMImage:
        model = session.get(VMImageModel, image_id)
        if model is None:
            raise ImageNotFoundError(f"VM image {image_id} not found")
        return _to_entity(model)
