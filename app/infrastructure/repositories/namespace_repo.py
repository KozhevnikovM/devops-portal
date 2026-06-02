from uuid import UUID, uuid4

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Namespace
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import BookingModel, NamespaceModel, UserModel

# A namespace is "held" while a booking referencing it is in a non-terminal state.
_LIVE_STATUSES = [
    s.value for s in BookingStatus if s not in (BookingStatus.RELEASED, BookingStatus.FAILED)
]


def _to_entity(m: NamespaceModel) -> Namespace:
    return Namespace(
        id=m.id,
        name=m.name,
        cluster_name=m.cluster_name,
        api_url=m.api_url,
        is_active=m.is_active,
        created_at=m.created_at,
    )


def _held_subquery():
    """namespace_ids currently held by a live booking."""
    return (
        select(BookingModel.namespace_id)
        .where(
            BookingModel.namespace_id.is_not(None),
            BookingModel.status.in_(_LIVE_STATUSES),
        )
    )


class NamespaceRepository:
    async def list_all(self, session: AsyncSession) -> list[Namespace]:
        result = await session.execute(select(NamespaceModel).order_by(NamespaceModel.name))
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[Namespace]:
        result = await session.execute(
            select(NamespaceModel)
            .where(NamespaceModel.is_active.is_(True))
            .order_by(NamespaceModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_available(self, session: AsyncSession) -> list[Namespace]:
        result = await session.execute(
            select(NamespaceModel)
            .where(
                NamespaceModel.is_active.is_(True),
                NamespaceModel.id.not_in(_held_subquery()),
            )
            .order_by(NamespaceModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def held_by(self, session: AsyncSession) -> dict[UUID, str | None]:
        """Map namespace_id → owner username for currently-held namespaces."""
        result = await session.execute(
            select(BookingModel.namespace_id, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .where(
                BookingModel.namespace_id.is_not(None),
                BookingModel.status.in_(_LIVE_STATUSES),
            )
        )
        return {ns_id: username for ns_id, username in result.all()}

    async def get(self, session: AsyncSession, namespace_id: UUID) -> Namespace:
        model = await session.get(NamespaceModel, namespace_id)
        if model is None:
            raise ValueError(f"Namespace {namespace_id} not found")
        return _to_entity(model)

    async def create(
        self, session: AsyncSession, name: str, cluster_name: str, api_url: str | None
    ) -> Namespace:
        model = NamespaceModel(
            id=uuid4(), name=name, cluster_name=cluster_name, api_url=api_url or None
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def update(self, session: AsyncSession, namespace_id: UUID, fields: dict) -> Namespace:
        model = await session.get(NamespaceModel, namespace_id)
        if model is None:
            raise ValueError(f"Namespace {namespace_id} not found")
        for key, value in fields.items():
            setattr(model, key, value)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, namespace_id: UUID) -> None:
        model = await session.get(NamespaceModel, namespace_id)
        if model is None:
            raise ValueError(f"Namespace {namespace_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, namespace_id: UUID) -> None:
        model = await session.get(NamespaceModel, namespace_id)
        if model is None:
            raise ValueError(f"Namespace {namespace_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, namespace_id: UUID) -> None:
        model = await session.get(NamespaceModel, namespace_id)
        if model is None:
            raise ValueError(f"Namespace {namespace_id} not found")
        await session.delete(model)
        await session.commit()

    async def lock_for_allocation(
        self, session: AsyncSession, namespace_id: UUID
    ) -> NamespaceModel | None:
        """SELECT … FOR UPDATE the namespace row — serializes concurrent allocation."""
        return await session.get(NamespaceModel, namespace_id, with_for_update=True)

    async def is_held(self, session: AsyncSession, namespace_id: UUID) -> bool:
        """True if a live (non-terminal) booking currently holds this namespace."""
        result = await session.execute(
            select(BookingModel.id)
            .where(
                BookingModel.namespace_id == namespace_id,
                BookingModel.status.in_(_LIVE_STATUSES),
            )
            .limit(1)
        )
        return result.first() is not None
