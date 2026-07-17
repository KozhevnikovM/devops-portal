from uuid import UUID, uuid4

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.booking_status import LIVE_STATUSES
from app.domain.entities import StaticVM
from app.infrastructure.database.models import BookingModel, StaticVMModel, UserModel

_LIVE_STATUSES = [s.value for s in LIVE_STATUSES]


def _to_entity(m: StaticVMModel) -> StaticVM:
    return StaticVM(
        id=m.id,
        name=m.name,
        host=m.host,
        username=m.username,
        password=m.password,
        ssh_key=m.ssh_key,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        is_active=m.is_active,
        created_at=m.created_at,
    )


def _held_subquery():
    """static_vm_ids currently held by a live booking."""
    return (
        select(BookingModel.static_vm_id)
        .where(
            BookingModel.static_vm_id.is_not(None),
            BookingModel.status.in_(_LIVE_STATUSES),
        )
    )


class StaticVMRepository:
    async def list_all(self, session: AsyncSession) -> list[StaticVM]:
        result = await session.execute(select(StaticVMModel).order_by(StaticVMModel.name))
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[StaticVM]:
        result = await session.execute(
            select(StaticVMModel)
            .where(StaticVMModel.is_active.is_(True))
            .order_by(StaticVMModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_available(self, session: AsyncSession) -> list[StaticVM]:
        result = await session.execute(
            select(StaticVMModel)
            .where(
                StaticVMModel.is_active.is_(True),
                StaticVMModel.id.not_in(_held_subquery()),
            )
            .order_by(StaticVMModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def count_available(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(func.count(StaticVMModel.id))
            .where(
                StaticVMModel.is_active.is_(True),
                StaticVMModel.id.not_in(_held_subquery()),
            )
        )
        return result.scalar_one()

    async def held_by(self, session: AsyncSession) -> dict[UUID, str | None]:
        """Map static_vm_id → owner username for currently-held static VMs."""
        result = await session.execute(
            select(BookingModel.static_vm_id, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .where(
                BookingModel.static_vm_id.is_not(None),
                BookingModel.status.in_(_LIVE_STATUSES),
            )
        )
        return {vm_id: username for vm_id, username in result.all()}

    async def get(self, session: AsyncSession, static_vm_id: UUID) -> StaticVM:
        model = await session.get(StaticVMModel, static_vm_id)
        if model is None:
            raise ValueError(f"Static VM {static_vm_id} not found")
        return _to_entity(model)

    async def get_by_name(self, session: AsyncSession, name: str) -> StaticVM | None:
        """Resolve an *active* static VM by its (unique) name; None if no active match."""
        result = await session.execute(
            select(StaticVMModel).where(
                StaticVMModel.name == name,
                StaticVMModel.is_active.is_(True),
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def create(
        self,
        session: AsyncSession,
        name: str,
        host: str,
        username: str,
        password: str | None,
        ssh_key: str | None,
        cpus: int | None,
        memory_mb: int | None,
    ) -> StaticVM:
        model = StaticVMModel(
            id=uuid4(),
            name=name,
            host=host,
            username=username,
            password=password,
            ssh_key=ssh_key,
            cpus=cpus,
            memory_mb=memory_mb,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def update(self, session: AsyncSession, static_vm_id: UUID, fields: dict) -> StaticVM:
        model = await session.get(StaticVMModel, static_vm_id)
        if model is None:
            raise ValueError(f"Static VM {static_vm_id} not found")
        for key, value in fields.items():
            setattr(model, key, value)
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, static_vm_id: UUID) -> None:
        model = await session.get(StaticVMModel, static_vm_id)
        if model is None:
            raise ValueError(f"Static VM {static_vm_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, static_vm_id: UUID) -> None:
        model = await session.get(StaticVMModel, static_vm_id)
        if model is None:
            raise ValueError(f"Static VM {static_vm_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, static_vm_id: UUID) -> None:
        model = await session.get(StaticVMModel, static_vm_id)
        if model is None:
            raise ValueError(f"Static VM {static_vm_id} not found")
        await session.delete(model)
        await session.commit()

    async def lock_for_allocation(
        self, session: AsyncSession, static_vm_id: UUID
    ) -> StaticVMModel | None:
        """SELECT … FOR UPDATE the static VM row — serializes concurrent allocation."""
        return await session.get(StaticVMModel, static_vm_id, with_for_update=True)

    async def lock_next_available(self, session: AsyncSession) -> StaticVMModel | None:
        """Reserve the next free static VM from the pool.

        `FOR UPDATE SKIP LOCKED` lets concurrent reservations each grab a different
        free row — no double-allocation, no lock contention. Returns None when the
        pool is exhausted.
        """
        result = await session.execute(
            select(StaticVMModel)
            .where(
                StaticVMModel.is_active.is_(True),
                StaticVMModel.id.not_in(_held_subquery()),
            )
            .order_by(StaticVMModel.name)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

    async def is_held(self, session: AsyncSession, static_vm_id: UUID) -> bool:
        """True if a live (non-terminal) booking currently holds this static VM."""
        result = await session.execute(
            select(BookingModel.id)
            .where(
                BookingModel.static_vm_id == static_vm_id,
                BookingModel.status.in_(_LIVE_STATUSES),
            )
            .limit(1)
        )
        return result.first() is not None
