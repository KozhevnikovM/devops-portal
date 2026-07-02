"""Repository for namespace_shares — tracks who a namespace booking is shared with."""
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import NamespaceShare
from app.infrastructure.database.models import (
    BookingModel, NamespaceModel, NamespaceShareModel, UserModel,
)


def _to_entity(model: NamespaceShareModel, username: str) -> NamespaceShare:
    return NamespaceShare(
        id=model.id,
        booking_id=model.booking_id,
        shared_with_user_id=model.shared_with_user_id,
        shared_with_username=username,
        created_at=model.created_at,
    )


class NamespaceShareRepository:
    async def create(
        self, session: AsyncSession, booking_id: UUID, shared_with_user_id: UUID
    ) -> NamespaceShare:
        """Create a new share row. Raises IntegrityError on duplicate."""
        # Fetch the username for the returned entity.
        user = await session.get(UserModel, shared_with_user_id)
        username = user.username if user else str(shared_with_user_id)

        model = NamespaceShareModel(
            id=uuid4(),
            booking_id=booking_id,
            shared_with_user_id=shared_with_user_id,
        )
        session.add(model)
        await session.flush()  # raises IntegrityError on duplicate before commit
        await session.refresh(model)
        return _to_entity(model, username)

    async def get(
        self, session: AsyncSession, booking_id: UUID, shared_with_user_id: UUID
    ) -> NamespaceShare | None:
        result = await session.execute(
            select(NamespaceShareModel, UserModel.username)
            .join(UserModel, UserModel.id == NamespaceShareModel.shared_with_user_id)
            .where(
                NamespaceShareModel.booking_id == booking_id,
                NamespaceShareModel.shared_with_user_id == shared_with_user_id,
            )
        )
        row = result.first()
        if row is None:
            return None
        model, username = row
        return _to_entity(model, username)

    async def list_by_booking(
        self, session: AsyncSession, booking_id: UUID
    ) -> list[NamespaceShare]:
        """Return all shares for a booking, joined with the recipient's username."""
        result = await session.execute(
            select(NamespaceShareModel, UserModel.username)
            .join(UserModel, UserModel.id == NamespaceShareModel.shared_with_user_id)
            .where(NamespaceShareModel.booking_id == booking_id)
            .order_by(NamespaceShareModel.created_at)
        )
        return [_to_entity(m, u) for m, u in result.all()]

    async def delete(
        self, session: AsyncSession, booking_id: UUID, shared_with_user_id: UUID
    ) -> bool:
        """Delete a share row. Returns True if a row was deleted, False if not found."""
        result = await session.execute(
            select(NamespaceShareModel).where(
                NamespaceShareModel.booking_id == booking_id,
                NamespaceShareModel.shared_with_user_id == shared_with_user_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await session.delete(model)
        await session.flush()
        return True

    async def list_shared_with_user(
        self, session: AsyncSession, user_id: UUID
    ) -> list[dict]:
        """Return namespace booking info for all bookings shared with `user_id`.

        Joins NamespaceShareModel → BookingModel → NamespaceModel → UserModel (owner).
        Includes environment_id if the booking belongs to an environment.
        """
        OwnerUser = UserModel.__table__.alias("owner_users")

        result = await session.execute(
            select(
                BookingModel.id,
                BookingModel.status,
                BookingModel.environment_id,
                NamespaceModel.name.label("namespace_name"),
                NamespaceModel.cluster_name,
                NamespaceModel.api_url,
                UserModel.username.label("owner_username"),
            )
            .join(NamespaceShareModel, NamespaceShareModel.booking_id == BookingModel.id)
            .join(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id, isouter=True)
            .join(UserModel, UserModel.username == BookingModel.user_id, isouter=True)
            .where(NamespaceShareModel.shared_with_user_id == user_id)
            .order_by(BookingModel.id)
        )
        rows = result.all()

        # Now fetch environment info for rows that have environment_id.
        from app.infrastructure.database.models import EnvironmentModel  # noqa: PLC0415
        env_ids = {r.environment_id for r in rows if r.environment_id}
        env_map: dict = {}
        if env_ids:
            env_result = await session.execute(
                select(EnvironmentModel).where(EnvironmentModel.id.in_(env_ids))
            )
            for env in env_result.scalars().all():
                env_map[env.id] = env

        output = []
        for row in rows:
            entry = {
                "booking_id": str(row.id),
                "status": row.status,
                "namespace": row.namespace_name,
                "cluster": row.cluster_name,
                "api_url": row.api_url,
                "owner_username": row.owner_username,
                "environment": None,
            }
            if row.environment_id and row.environment_id in env_map:
                env = env_map[row.environment_id]
                entry["environment"] = {
                    "id": str(env.id),
                    "name": env.name,
                }
            output.append(entry)
        return output
