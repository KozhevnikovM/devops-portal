from uuid import UUID, uuid4

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Environment
from app.infrastructure.database.models import BookingModel, EnvironmentModel, UserModel
from app.infrastructure.repositories.booking_repo import _to_entity as _booking_to_entity


def _to_entity(m: EnvironmentModel, bookings=None, owner_username=None) -> Environment:
    return Environment(
        id=m.id, name=m.name, blueprint_name=m.blueprint_name, user_id=m.user_id,
        ttl_minutes=m.ttl_minutes, expires_at=m.expires_at, created_at=m.created_at,
        bookings=bookings or [], owner_username=owner_username,
    )


class EnvironmentRepository:
    async def create(
        self, session: AsyncSession, name: str, blueprint_name: str | None,
        user_id: str, ttl_minutes: int, expires_at,
    ) -> Environment:
        model = EnvironmentModel(
            id=uuid4(), name=name, blueprint_name=blueprint_name, user_id=user_id,
            ttl_minutes=ttl_minutes, expires_at=expires_at,
        )
        session.add(model)
        await session.flush()  # need the id for child bookings before commit
        return _to_entity(model)

    async def delete(self, session: AsyncSession, environment_id: UUID) -> None:
        model = await session.get(EnvironmentModel, environment_id)
        if model is not None:
            await session.delete(model)
            await session.commit()

    async def _children(self, session: AsyncSession, environment_id: UUID):
        result = await session.execute(
            select(BookingModel, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .where(BookingModel.environment_id == environment_id)
            .order_by(BookingModel.created_at)
        )
        return [_booking_to_entity(b, owner_username=u) for b, u in result.all()]

    async def get(self, session: AsyncSession, environment_id: UUID) -> Environment:
        result = await session.execute(
            select(EnvironmentModel, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .where(EnvironmentModel.id == environment_id)
        )
        row = result.first()
        if row is None:
            raise ValueError(f"Environment {environment_id} not found")
        model, owner = row
        children = await self._children(session, environment_id)
        return _to_entity(model, bookings=children, owner_username=owner)

    async def list_all(self, session: AsyncSession) -> list[Environment]:
        return await self._list(session, None)

    async def list_by_user(self, session: AsyncSession, user_id: str) -> list[Environment]:
        return await self._list(session, user_id)

    async def _list(self, session: AsyncSession, user_id: str | None) -> list[Environment]:
        stmt = (
            select(EnvironmentModel, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .order_by(EnvironmentModel.created_at.desc())
        )
        if user_id is not None:
            stmt = stmt.where(EnvironmentModel.user_id == user_id)
        result = await session.execute(stmt)
        envs = []
        for model, owner in result.all():
            children = await self._children(session, model.id)
            envs.append(_to_entity(model, bookings=children, owner_username=owner))
        return envs
