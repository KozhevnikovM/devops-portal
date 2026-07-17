from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from app.domain.booking_status import LIVE_CHILD_STATUSES
from app.domain.entities import Environment
from app.domain.enums import BookingStatus, ResourceType
from app.domain.lease import Lease
from app.infrastructure.database.models import (
    BookingModel, EnvironmentModel, NamespaceModel, StaticVMModel, UserModel,
)
from app.infrastructure.repositories.booking_repo import _to_entity as _booking_to_entity

# Second alias of users to resolve created_by (the dispatcher) → username, distinct from the owner.
_CreatorUser = aliased(UserModel)


def _lease_until(ttl_minutes: int) -> datetime:
    """The deadline for a lease of ttl_minutes starting now (permanent when ttl is 0)."""
    return Lease.starting_now(ttl_minutes).expires_at


def _stamp_lease_if_all_ready(session: Session, env: EnvironmentModel) -> bool:
    """If every child of `env` is READY, start the whole stack's lease now (env + each child share
    one deadline). No-op (returns False) if there are no children or any child isn't READY yet."""
    children = list(session.execute(
        select(BookingModel).where(BookingModel.environment_id == env.id)
    ).scalars().all())
    if not children or any(c.status != BookingStatus.READY.value for c in children):
        return False
    deadline = _lease_until(env.ttl_minutes)
    env.expires_at = deadline
    for c in children:
        c.expires_at = deadline
    return True

_LIVE_CHILD_STATUSES = [s.value for s in LIVE_CHILD_STATUSES]


def _to_entity(m: EnvironmentModel, bookings=None, owner_username=None, created_by_username=None) -> Environment:
    return Environment(
        id=m.id, name=m.name, blueprint_name=m.blueprint_name, user_id=m.user_id,
        ttl_minutes=m.ttl_minutes, expires_at=m.expires_at, created_at=m.created_at,
        bookings=bookings or [], owner_username=owner_username, created_by=m.created_by,
        created_by_username=created_by_username,
    )


class EnvironmentRepository:
    async def create(
        self, session: AsyncSession, name: str, blueprint_name: str | None,
        user_id: str, ttl_minutes: int, expires_at, created_by: str | None = None,
    ) -> Environment:
        model = EnvironmentModel(
            id=uuid4(), name=name, blueprint_name=blueprint_name, user_id=user_id,
            ttl_minutes=ttl_minutes, expires_at=expires_at, created_by=created_by,
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
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .where(BookingModel.environment_id == environment_id)
            .order_by(BookingModel.created_at)
        )
        return [
            _booking_to_entity(b, owner_username=u, namespace=ns, static_vm=svm)
            for b, u, ns, svm in result.all()
        ]

    async def get(self, session: AsyncSession, environment_id: UUID) -> Environment:
        result = await session.execute(
            select(EnvironmentModel, UserModel.username, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == EnvironmentModel.created_by)
            .where(EnvironmentModel.id == environment_id)
        )
        row = result.first()
        if row is None:
            raise ValueError(f"Environment {environment_id} not found")
        model, owner, creator = row
        children = await self._children(session, environment_id)
        return _to_entity(model, bookings=children, owner_username=owner, created_by_username=creator)

    async def get_by_namespace(
        self, session: AsyncSession, namespace_name: str, cluster_name: str | None = None,
    ) -> list[Environment]:
        """Environments currently holding a namespace named `namespace_name` via a live child.

        Matches only namespace bookings that belong to an environment and aren't terminal
        (RELEASED/FAILED); QUEUED children hold no namespace so the join excludes them. Returns the
        distinct environments — 0, 1, or (the same name on different clusters) several.
        """
        stmt = (
            select(BookingModel.environment_id)
            .join(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .where(
                BookingModel.resource_type == ResourceType.NAMESPACE.value,
                BookingModel.environment_id.is_not(None),
                BookingModel.status.notin_(
                    [BookingStatus.RELEASED.value, BookingStatus.FAILED.value]
                ),
                NamespaceModel.name == namespace_name,
            )
            .distinct()
        )
        if cluster_name is not None:
            stmt = stmt.where(NamespaceModel.cluster_name == cluster_name)
        env_ids = (await session.execute(stmt)).scalars().all()
        return [await self.get(session, env_id) for env_id in env_ids]

    async def list_all(self, session: AsyncSession) -> list[Environment]:
        return await self._list(session, None)

    async def list_by_user(self, session: AsyncSession, user_id: str) -> list[Environment]:
        return await self._list(session, user_id)

    async def _list(self, session: AsyncSession, user_id: str | None) -> list[Environment]:
        stmt = (
            select(EnvironmentModel, UserModel.username, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == EnvironmentModel.created_by)
            .order_by(EnvironmentModel.created_at.desc())
        )
        if user_id is not None:
            # Visible to user: owned, plus any dispatched on someone's behalf (created_by).
            stmt = stmt.where(
                or_(EnvironmentModel.user_id == user_id, EnvironmentModel.created_by == user_id)
            )
        result = await session.execute(stmt)
        envs = []
        for model, owner, creator in result.all():
            children = await self._children(session, model.id)
            envs.append(_to_entity(model, bookings=children, owner_username=owner, created_by_username=creator))
        return envs

    # ── Sync helpers (Celery beat — env-aware TTL enforcement) ──────────────────
    def sync_list_expired(self, session: Session) -> list[Environment]:
        """Return environments past their expires_at that still have at least one live child."""
        live_child = (
            select(BookingModel.environment_id)
            .where(BookingModel.status.in_(_LIVE_CHILD_STATUSES),
                   BookingModel.environment_id.is_not(None))
        )
        result = session.execute(
            select(EnvironmentModel).where(
                EnvironmentModel.expires_at < datetime.now(timezone.utc),
                EnvironmentModel.id.in_(live_child),
            )
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_live_children(self, session: Session, environment_id: UUID):
        """Return the still-live child bookings of an environment (for grouped teardown)."""
        result = session.execute(
            select(BookingModel).where(
                BookingModel.environment_id == environment_id,
                BookingModel.status.in_(_LIVE_CHILD_STATUSES),
            )
        )
        return [_booking_to_entity(m) for m in result.scalars().all()]

    # ── Lease start: whole-stack TTL begins once every child is READY (#223) ────
    async def start_lease_if_ready(self, session: AsyncSession, environment_id: UUID) -> bool:
        """Async path (ordering): stamp the lease if the environment is already fully READY."""
        env = await session.get(EnvironmentModel, environment_id)
        if env is None:
            return False
        children = (await session.execute(
            select(BookingModel).where(BookingModel.environment_id == environment_id)
        )).scalars().all()
        if not children or any(c.status != BookingStatus.READY.value for c in children):
            return False
        deadline = _lease_until(env.ttl_minutes)
        env.expires_at = deadline
        for c in children:
            c.expires_at = deadline
        await session.commit()
        return True

    def sync_start_lease_if_ready_for_booking(self, session: Session, booking_id: UUID) -> bool:
        """Sync path (provision task): if this booking belongs to an environment whose children are
        now all READY, start the whole stack's lease. No-op for a standalone booking."""
        booking = session.get(BookingModel, booking_id)
        if booking is None or booking.environment_id is None:
            return False
        env = session.get(EnvironmentModel, booking.environment_id)
        if env is None:
            return False
        stamped = _stamp_lease_if_all_ready(session, env)
        if stamped:
            session.commit()
        return stamped
