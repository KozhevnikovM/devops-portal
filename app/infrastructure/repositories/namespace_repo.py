from uuid import UUID, uuid4

from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Namespace
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import BookingModel, NamespaceModel, NamespaceShareModel, UserModel

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

    async def get_by_name(self, session: AsyncSession, name: str) -> list[Namespace]:
        """All namespaces with this name (may span multiple clusters)."""
        result = await session.execute(
            select(NamespaceModel).where(NamespaceModel.name == name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get_by_name_and_cluster(
        self, session: AsyncSession, name: str, cluster_name: str
    ) -> Namespace | None:
        """Resolve a namespace by its (name, cluster) identity. None if no match."""
        result = await session.execute(
            select(NamespaceModel).where(
                NamespaceModel.name == name,
                NamespaceModel.cluster_name == cluster_name,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

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

    async def lock_next_available(self, session: AsyncSession) -> NamespaceModel | None:
        """Reserve the next free namespace from the pool.

        `FOR UPDATE SKIP LOCKED` lets concurrent reservations each grab a different
        free row — no double-allocation. Returns None when the pool is exhausted.
        """
        result = await session.execute(
            select(NamespaceModel)
            .where(
                NamespaceModel.is_active.is_(True),
                NamespaceModel.id.not_in(_held_subquery()),
            )
            .order_by(NamespaceModel.name)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

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

    async def list_held_standalone_by_user(
        self, session: AsyncSession, user_id: str
    ) -> list[Namespace]:
        """Active namespaces currently held by a live, standalone booking of user_id.

        A booking is standalone when environment_id IS NULL. QUEUED bookings hold no namespace
        yet (namespace_id is None), so they never match the join.
        """
        result = await session.execute(
            select(NamespaceModel)
            .join(BookingModel, BookingModel.namespace_id == NamespaceModel.id)
            .where(
                cast(BookingModel.user_id, String) == user_id,
                BookingModel.status.in_(_LIVE_STATUSES),
                BookingModel.environment_id.is_(None),
                NamespaceModel.is_active.is_(True),
            )
            .order_by(NamespaceModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_shared_standalone_namespaces(
        self, session: AsyncSession, user_id: UUID,
    ) -> list[Namespace]:
        """Active namespaces held by a live standalone booking that is shared with user_id.

        Excludes bookings already adopted into an environment (environment_id IS NOT NULL),
        since those are no longer independently selectable.
        """
        result = await session.execute(
            select(NamespaceModel)
            .join(BookingModel, BookingModel.namespace_id == NamespaceModel.id)
            .join(NamespaceShareModel, NamespaceShareModel.booking_id == BookingModel.id)
            .where(
                NamespaceModel.is_active.is_(True),
                BookingModel.status.in_(_LIVE_STATUSES),
                BookingModel.environment_id.is_(None),
                NamespaceShareModel.shared_with_user_id == user_id,
            )
            .order_by(NamespaceModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]
