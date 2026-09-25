from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from app.domain.booking_status import CAN_BECOME_READY, LIVE_CHILD_STATUSES
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Environment
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import EnvironmentNotFoundError
from app.domain.lease import Lease, lease_can_start
from app.infrastructure.database.models import (
    BookingModel, EnvironmentModel, NamespaceModel, StaticVMModel, UserModel,
)
from app.infrastructure.repositories.booking_repo import _to_entity as _booking_to_entity

# Second alias of users to resolve created_by (the dispatcher) → username, distinct from the owner.
_CreatorUser = aliased(UserModel)


def _lease_until(ttl_minutes: int) -> datetime:
    """The deadline for a lease of ttl_minutes starting now (permanent when ttl is 0)."""
    return Lease.starting_now(ttl_minutes).expires_at


def _lease_already_started(env: EnvironmentModel) -> bool:
    """A timed environment whose expiry is no longer the placeholder has had its lease started."""
    return env.ttl_minutes > 0 and env.expires_at != PERMANENT_EXPIRES_AT


def _stamp_lease(env: EnvironmentModel, children) -> bool:
    """Start the whole stack's lease (env + every child share one deadline) if its children have
    settled and it hasn't started yet (#223, #434). Caller must hold the env row lock and commit."""
    if not lease_can_start(BookingStatus(c.status) for c in children) or _lease_already_started(env):
        return False
    deadline = _lease_until(env.ttl_minutes)
    env.expires_at = deadline
    for c in children:
        c.expires_at = deadline
    return True


def _children_stmt(environment_id: UUID):
    # populate_existing: re-read statuses committed by other workers since this session loaded them.
    return (
        select(BookingModel).where(BookingModel.environment_id == environment_id)
        .execution_options(populate_existing=True)
    )


# Child statuses that can still become READY — an environment with one isn't lease-eligible (#434).
_IN_FLIGHT_VALUES = [s.value for s in CAN_BECOME_READY]
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

    async def update_name(self, session: AsyncSession, environment_id: UUID, name: str) -> None:
        model = await session.get(EnvironmentModel, environment_id)
        if model is None:
            raise EnvironmentNotFoundError(f"Environment {environment_id} not found")
        model.name = name
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

    async def _children_batch(
        self, session: AsyncSession, env_ids: list[UUID],
    ) -> dict[UUID, list]:
        """Fetch all children for a batch of environment IDs in a single query."""
        if not env_ids:
            return {}
        result = await session.execute(
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .where(BookingModel.environment_id.in_(env_ids))
            .order_by(BookingModel.environment_id, BookingModel.created_at)
        )
        grouped: dict[UUID, list] = {eid: [] for eid in env_ids}
        for b, u, ns, svm in result.all():
            grouped[b.environment_id].append(
                _booking_to_entity(b, owner_username=u, namespace=ns, static_vm=svm)
            )
        return grouped

    async def get(self, session: AsyncSession, environment_id: UUID) -> Environment:
        result = await session.execute(
            select(EnvironmentModel, UserModel.username, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == EnvironmentModel.created_by)
            .where(EnvironmentModel.id == environment_id)
        )
        row = result.first()
        if row is None:
            raise EnvironmentNotFoundError(f"Environment {environment_id} not found")
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
        env_ids = list((await session.execute(stmt)).scalars().all())
        if not env_ids:
            return []
        env_result = await session.execute(
            select(EnvironmentModel, UserModel.username, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == EnvironmentModel.user_id, isouter=True)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == EnvironmentModel.created_by)
            .where(EnvironmentModel.id.in_(env_ids))
        )
        env_rows = env_result.all()
        children_by_env = await self._children_batch(session, env_ids)
        return [
            _to_entity(model, bookings=children_by_env.get(model.id, []),
                       owner_username=owner, created_by_username=creator)
            for model, owner, creator in env_rows
        ]

    async def list_all(self, session: AsyncSession, label: str | None = None) -> list[Environment]:
        return await self._list(session, None, label=label)

    async def list_by_user(
        self, session: AsyncSession, user_id: str, label: str | None = None,
    ) -> list[Environment]:
        return await self._list(session, user_id, label=label)

    async def _list(
        self, session: AsyncSession, user_id: str | None, label: str | None = None,
    ) -> list[Environment]:
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
        if label is not None and label.strip():
            # An environment's name already serves as its label (#345); filter on it directly.
            stmt = stmt.where(EnvironmentModel.name.ilike(f"%{label.strip()}%"))
        rows = (await session.execute(stmt)).all()
        env_ids = [model.id for model, _, _ in rows]
        children_by_env = await self._children_batch(session, env_ids)
        return [
            _to_entity(model, bookings=children_by_env.get(model.id, []),
                       owner_username=owner, created_by_username=creator)
            for model, owner, creator in rows
        ]

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

    # ── Lease start: whole-stack TTL begins once every child has settled (#223, #434) ────
    # Every path locks the environment row first (SELECT … FOR UPDATE) and only then reads the
    # children and the expiry, so concurrent settling events start the lease exactly once. Each path
    # commits, which releases the lock, even when it stamps nothing.
    async def start_lease_if_ready(self, session: AsyncSession, environment_id: UUID) -> bool:
        """Async path (ordering, promotion): start the lease if the children have settled."""
        env = await session.get(
            EnvironmentModel, environment_id, with_for_update=True, populate_existing=True,
        )
        if env is None:
            return False
        children = (await session.execute(_children_stmt(environment_id))).scalars().all()
        stamped = _stamp_lease(env, children)
        await session.commit()
        return stamped

    async def start_lease_if_ready_for_booking(self, session: AsyncSession, booking_id: UUID) -> bool:
        """Async twin of sync_start_lease_if_ready_for_booking. No-op for a standalone booking."""
        booking = await session.get(BookingModel, booking_id)
        if booking is None or booking.environment_id is None:
            return False
        return await self.start_lease_if_ready(session, booking.environment_id)

    def sync_start_lease_if_ready(self, session: Session, environment_id: UUID) -> bool:
        """Sync path (Celery): start the lease if the children have settled."""
        env = session.get(EnvironmentModel, environment_id, with_for_update=True, populate_existing=True)
        if env is None:
            return False
        children = session.execute(_children_stmt(environment_id)).scalars().all()
        stamped = _stamp_lease(env, children)
        session.commit()
        return stamped

    def sync_start_lease_if_ready_for_booking(self, session: Session, booking_id: UUID) -> bool:
        """Sync path (provision task, reaper, promotion): if this booking belongs to an environment
        whose children have now settled, start the whole stack's lease. No-op for a standalone booking."""
        booking = session.get(BookingModel, booking_id)
        if booking is None or booking.environment_id is None:
            return False
        return self.sync_start_lease_if_ready(session, booking.environment_id)

    def sync_list_lease_pending(self, session: Session) -> list[UUID]:
        """Ids of timed environments still on the placeholder expiry whose children have settled
        (at least one READY, none that can still become READY) — for lease reconciliation (#434).
        Only a preselection: sync_start_lease_if_ready re-checks under the row lock."""
        ready_child = select(BookingModel.environment_id).where(
            BookingModel.status == BookingStatus.READY.value,
            BookingModel.environment_id.is_not(None),
        )
        in_flight_child = select(BookingModel.environment_id).where(
            BookingModel.status.in_(_IN_FLIGHT_VALUES),
            BookingModel.environment_id.is_not(None),
        )
        result = session.execute(
            select(EnvironmentModel.id).where(
                EnvironmentModel.expires_at == PERMANENT_EXPIRES_AT,
                EnvironmentModel.ttl_minutes > 0,
                EnvironmentModel.id.in_(ready_child),
                EnvironmentModel.id.not_in(in_flight_child),
            )
        )
        return list(result.scalars().all())
