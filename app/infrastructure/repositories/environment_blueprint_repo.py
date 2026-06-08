from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.entities import EnvironmentBlueprint, EnvironmentBlueprintItem
from app.infrastructure.database.models import (
    EnvironmentBlueprintItemModel, EnvironmentBlueprintModel,
)


def _item_to_entity(m: EnvironmentBlueprintItemModel) -> EnvironmentBlueprintItem:
    return EnvironmentBlueprintItem(
        id=m.id, resource_type=m.resource_type, position=m.position,
        label=m.label, spec=m.spec or {},
    )


def _to_entity(m: EnvironmentBlueprintModel) -> EnvironmentBlueprint:
    return EnvironmentBlueprint(
        id=m.id, name=m.name, description=m.description, is_active=m.is_active,
        created_at=m.created_at,
        items=[_item_to_entity(i) for i in sorted(m.items, key=lambda x: x.position)],
    )


def _item_models(items: list[dict]) -> list[EnvironmentBlueprintItemModel]:
    return [
        EnvironmentBlueprintItemModel(
            id=uuid4(),
            resource_type=item["resource_type"],
            position=item.get("position", idx),
            label=item.get("label") or None,
            spec=item.get("spec") or {},
        )
        for idx, item in enumerate(items)
    ]


class EnvironmentBlueprintRepository:
    _opts = (selectinload(EnvironmentBlueprintModel.items),)

    async def list_all(self, session: AsyncSession) -> list[EnvironmentBlueprint]:
        result = await session.execute(
            select(EnvironmentBlueprintModel).options(*self._opts).order_by(EnvironmentBlueprintModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[EnvironmentBlueprint]:
        result = await session.execute(
            select(EnvironmentBlueprintModel).options(*self._opts)
            .where(EnvironmentBlueprintModel.is_active.is_(True))
            .order_by(EnvironmentBlueprintModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get(self, session: AsyncSession, blueprint_id: UUID) -> EnvironmentBlueprint:
        result = await session.execute(
            select(EnvironmentBlueprintModel).options(*self._opts)
            .where(EnvironmentBlueprintModel.id == blueprint_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            raise ValueError(f"Environment blueprint {blueprint_id} not found")
        return _to_entity(model)

    async def get_by_name(self, session: AsyncSession, name: str) -> EnvironmentBlueprint | None:
        result = await session.execute(
            select(EnvironmentBlueprintModel).options(*self._opts)
            .where(EnvironmentBlueprintModel.name == name, EnvironmentBlueprintModel.is_active.is_(True))
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def create(
        self, session: AsyncSession, name: str, description: str | None, items: list[dict],
    ) -> EnvironmentBlueprint:
        model = EnvironmentBlueprintModel(
            id=uuid4(), name=name, description=description or None, items=_item_models(items),
        )
        session.add(model)
        await session.commit()
        return await self.get(session, model.id)

    async def update(
        self, session: AsyncSession, blueprint_id: UUID, fields: dict, items: list[dict] | None = None,
    ) -> EnvironmentBlueprint:
        result = await session.execute(
            select(EnvironmentBlueprintModel).options(*self._opts)
            .where(EnvironmentBlueprintModel.id == blueprint_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            raise ValueError(f"Environment blueprint {blueprint_id} not found")
        for key, value in fields.items():
            setattr(model, key, value)
        if items is not None:
            model.items = _item_models(items)  # replace the whole set (delete-orphan handles removal)
        await session.commit()
        return await self.get(session, blueprint_id)

    async def activate(self, session: AsyncSession, blueprint_id: UUID) -> None:
        await self._set_active(session, blueprint_id, True)

    async def deactivate(self, session: AsyncSession, blueprint_id: UUID) -> None:
        await self._set_active(session, blueprint_id, False)

    async def _set_active(self, session: AsyncSession, blueprint_id: UUID, active: bool) -> None:
        model = await session.get(EnvironmentBlueprintModel, blueprint_id)
        if model is None:
            raise ValueError(f"Environment blueprint {blueprint_id} not found")
        model.is_active = active
        await session.commit()

    async def delete(self, session: AsyncSession, blueprint_id: UUID) -> None:
        model = await session.get(EnvironmentBlueprintModel, blueprint_id)
        if model is None:
            raise ValueError(f"Environment blueprint {blueprint_id} not found")
        await session.delete(model)
        await session.commit()
