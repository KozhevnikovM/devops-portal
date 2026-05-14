from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import VMTemplate
from app.infrastructure.database.models import VMTemplateModel


def _to_entity(m: VMTemplateModel) -> VMTemplate:
    return VMTemplate(
        id=m.id,
        name=m.name,
        vapp_template_id=m.vapp_template_id,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        disk_mb=m.disk_mb,
        is_active=m.is_active,
        created_at=m.created_at,
    )


class TemplateRepository:
    async def list_active(self, session: AsyncSession) -> list[VMTemplate]:
        result = await session.execute(
            select(VMTemplateModel)
            .where(VMTemplateModel.is_active.is_(True))
            .order_by(VMTemplateModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get(self, session: AsyncSession, template_id: UUID) -> VMTemplate:
        model = await session.get(VMTemplateModel, template_id)
        if model is None or not model.is_active:
            raise ValueError(f"VM template {template_id} not found or inactive")
        return _to_entity(model)

    def sync_get(self, session: Session, template_id: UUID) -> VMTemplate:
        model = session.get(VMTemplateModel, template_id)
        if model is None:
            raise ValueError(f"VM template {template_id} not found")
        return _to_entity(model)
