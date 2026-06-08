from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import cast, func, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.database.models import (
    BookingAuditModel, BookingModel, NamespaceModel, StaticVMModel, UserModel,
)


def _to_audit_entity(m: BookingAuditModel) -> BookingAuditEntry:
    return BookingAuditEntry(
        id=m.id,
        booking_id=m.booking_id,
        actor_id=m.actor_id,
        action=m.action,
        old_status=m.old_status,
        new_status=m.new_status,
        metadata=m.extra,
        created_at=m.created_at,
    )


def _to_entity(
    m: BookingModel,
    owner_username: str | None = None,
    namespace: NamespaceModel | None = None,
    static_vm: StaticVMModel | None = None,
) -> Booking:
    return Booking(
        id=m.id,
        user_id=m.user_id,
        status=BookingStatus(m.status),
        resource_type=ResourceType(m.resource_type),
        ttl_minutes=m.ttl_minutes,
        expires_at=m.expires_at,
        created_at=m.created_at,
        image_id=m.image_id,
        image_name=m.image_name,
        hw_config_id=m.hw_config_id,
        hw_config_name=m.hw_config_name,
        vm_ip=m.vm_ip,
        vm_password=m.vm_password,
        owner_username=owner_username,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        disk_mb=m.disk_mb,
        drive_type=m.drive_type,
        status_message=m.status_message,
        namespace_id=m.namespace_id,
        namespace_name=namespace.name if namespace else None,
        cluster_name=namespace.cluster_name if namespace else None,
        api_url=namespace.api_url if namespace else None,
        static_vm_id=m.static_vm_id,
        static_vm_name=static_vm.name if static_vm else None,
        static_vm_host=static_vm.host if static_vm else None,
        static_vm_username=static_vm.username if static_vm else None,
        static_vm_password=static_vm.password if static_vm else None,
        static_vm_ssh_key=static_vm.ssh_key if static_vm else None,
    )


def _apply_resource_type_filter(stmt, resource_type: str | list[str] | None):
    """Filter by a single resource_type or any of a list (VM page wants VM + STATIC_VM)."""
    if resource_type is None:
        return stmt
    if isinstance(resource_type, (list, tuple, set)):
        return stmt.where(BookingModel.resource_type.in_(list(resource_type)))
    return stmt.where(BookingModel.resource_type == resource_type)


# Statuses in which a booking still holds its resource (everything but the terminal ones).
_POOLED_LIVE_STATUSES = [
    s.value for s in BookingStatus if s not in (BookingStatus.RELEASED, BookingStatus.FAILED)
]

# Pooled resource model + the booking FK that references it, keyed by resource_type.
_POOLED_RESOURCE = {
    ResourceType.STATIC_VM.value: (StaticVMModel, BookingModel.static_vm_id),
    ResourceType.NAMESPACE.value: (NamespaceModel, BookingModel.namespace_id),
}


def _free_resource_stmt(resource_type: str):
    """Next free resource of a pooled type, lockable (FOR UPDATE SKIP LOCKED)."""
    model, fk = _POOLED_RESOURCE[resource_type]
    held = select(fk).where(fk.is_not(None), BookingModel.status.in_(_POOLED_LIVE_STATUSES))
    return (
        select(model)
        .where(model.is_active.is_(True), model.id.not_in(held))
        .order_by(model.name)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _oldest_queued_stmt(resource_type: str):
    """Oldest QUEUED booking of a type, lockable so two frees can't promote the same one."""
    return (
        select(BookingModel)
        .where(
            BookingModel.resource_type == resource_type,
            BookingModel.status == BookingStatus.QUEUED.value,
        )
        .order_by(BookingModel.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _queue_rank_stmt(resource_type: str, created_at: datetime):
    return select(func.count(BookingModel.id)).where(
        BookingModel.resource_type == resource_type,
        BookingModel.status == BookingStatus.QUEUED.value,
        BookingModel.created_at < created_at,
    )


def _assign_resource_and_ready(session, booking_model, resource_type: str, resource) -> None:
    """Attach a freed resource to a QUEUED booking, flip it READY, start its TTL, audit."""
    _, fk = _POOLED_RESOURCE[resource_type]
    setattr(booking_model, fk.key, resource.id)
    old_status = booking_model.status
    booking_model.status = BookingStatus.READY.value
    if booking_model.ttl_minutes == 0:
        booking_model.expires_at = PERMANENT_EXPIRES_AT
    else:
        booking_model.expires_at = datetime.now(timezone.utc) + timedelta(minutes=booking_model.ttl_minutes)
    session.add(BookingAuditModel(
        booking_id=booking_model.id,
        actor_id="system",
        action="STATUS_CHANGED",
        old_status=old_status,
        new_status=BookingStatus.READY.value,
    ))


class BookingRepository:
    async def create(self, session: AsyncSession, booking: Booking) -> Booking:
        model = BookingModel(
            id=booking.id,
            user_id=booking.user_id,
            status=booking.status.value,
            ttl_minutes=booking.ttl_minutes,
            expires_at=booking.expires_at,
            created_at=booking.created_at,
            image_id=booking.image_id,
            image_name=booking.image_name,
            hw_config_id=booking.hw_config_id,
            hw_config_name=booking.hw_config_name,
            resource_type=booking.resource_type.value,
            namespace_id=booking.namespace_id,
            static_vm_id=booking.static_vm_id,
            cpus=booking.cpus,
            memory_mb=booking.memory_mb,
            disk_mb=booking.disk_mb,
            drive_type=booking.drive_type,
        )
        session.add(model)
        await session.flush()  # INSERT booking before audit to satisfy FK constraint
        session.add(BookingAuditModel(
            booking_id=booking.id,
            actor_id=booking.user_id,
            action="CREATED",
        ))
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def get(self, session: AsyncSession, booking_id: UUID) -> Booking:
        result = await session.execute(
            select(BookingModel, NamespaceModel, StaticVMModel)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .where(BookingModel.id == booking_id)
        )
        row = result.first()
        if row is None:
            raise BookingNotFoundError(booking_id)
        model, namespace, static_vm = row
        return _to_entity(model, namespace=namespace, static_vm=static_vm)

    async def update_status(
        self,
        session: AsyncSession,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
        vm_password: str | None = None,
        actor_id: str = "system",
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_status = model.status
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        if vm_password is not None:
            model.vm_password = vm_password
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
        await session.commit()

    async def list_all(
        self,
        session: AsyncSession,
        include_released: bool = False,
        resource_type: str | list[str] | None = None,
    ) -> list[Booking]:
        stmt = (
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .order_by(BookingModel.created_at.desc())
        )
        if not include_released:
            stmt = stmt.where(BookingModel.status != BookingStatus.RELEASED.value)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        result = await session.execute(stmt)
        return [_to_entity(m, username, ns, svm) for m, username, ns, svm in result.all()]

    async def list_by_user(
        self,
        session: AsyncSession,
        user_id: str,
        include_released: bool = False,
        resource_type: str | list[str] | None = None,
    ) -> list[Booking]:
        stmt = (
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .where(BookingModel.user_id == user_id)
            .order_by(BookingModel.created_at.desc())
        )
        if not include_released:
            stmt = stmt.where(BookingModel.status != BookingStatus.RELEASED.value)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        result = await session.execute(stmt)
        return [_to_entity(m, username, ns, svm) for m, username, ns, svm in result.all()]

    async def list_audit(self, session: AsyncSession, booking_id: UUID) -> list[BookingAuditEntry]:
        result = await session.execute(
            select(BookingAuditModel)
            .where(BookingAuditModel.booking_id == booking_id)
            .order_by(BookingAuditModel.created_at)
        )
        return [_to_audit_entity(m) for m in result.scalars().all()]

    async def extend(
        self,
        session: AsyncSession,
        booking_id: UUID,
        extend_minutes: int,
        actor_id: str,
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        if extend_minutes == 0:
            model.ttl_minutes = 0
            model.expires_at = PERMANENT_EXPIRES_AT
        else:
            model.expires_at = model.expires_at + timedelta(minutes=extend_minutes)
            model.ttl_minutes = model.ttl_minutes + extend_minutes
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="EXTENDED",
            extra={"extend_minutes": extend_minutes},
        ))
        await session.commit()

    async def promote_next_queued(self, session: AsyncSession, resource_type: str) -> Booking | None:
        """Assign the next free resource to the oldest QUEUED booking of this type → READY."""
        booking = (await session.execute(_oldest_queued_stmt(resource_type))).scalar_one_or_none()
        if booking is None:
            return None
        resource = (await session.execute(_free_resource_stmt(resource_type))).scalar_one_or_none()
        if resource is None:
            return None  # nothing free yet — stays queued
        _assign_resource_and_ready(session, booking, resource_type, resource)
        await session.commit()
        await session.refresh(booking)
        return _to_entity(booking)

    async def queue_position(self, session: AsyncSession, resource_type: str, created_at: datetime) -> int:
        result = await session.execute(_queue_rank_stmt(resource_type, created_at))
        return result.scalar_one() + 1

    # Sync variants used by Celery workers
    def sync_get(self, session: Session, booking_id: UUID) -> Booking:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        return _to_entity(model)

    def sync_update_status(
        self,
        session: Session,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
        vm_password: str | None = None,
        actor_id: str = "system",
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_status = model.status
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        if vm_password is not None:
            model.vm_password = vm_password
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
        session.commit()

    def sync_set_status_message(
        self, session: Session, booking_id: UUID, message: str | None
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.status_message = message
        session.commit()

    def sync_list_expired(self, session: Session) -> list[Booking]:
        """Return READY bookings whose expires_at is in the past."""
        result = session.execute(
            select(BookingModel).where(
                BookingModel.status == BookingStatus.READY.value,
                BookingModel.expires_at < datetime.now(timezone.utc),
            )
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_list_stale_provisioning(
        self, session: Session, threshold_minutes: int = 60
    ) -> list[Booking]:
        """Return PENDING/PROVISIONING/CONFIGURING/RETRY bookings older than threshold_minutes."""
        stale_statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
            BookingStatus.CONFIGURING.value,
            BookingStatus.RETRY.value,
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
        result = session.execute(
            select(BookingModel).where(
                BookingModel.status.in_(stale_statuses),
                BookingModel.created_at < cutoff,
            )
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_list_in_progress(self, session: Session) -> list[Booking]:
        """Return all PENDING/PROVISIONING/CONFIGURING/RETRY bookings regardless of age."""
        statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
            BookingStatus.CONFIGURING.value,
            BookingStatus.RETRY.value,
        ]
        result = session.execute(
            select(BookingModel).where(BookingModel.status.in_(statuses))
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_promote_next_queued(self, session: Session, resource_type: str) -> Booking | None:
        """Sync twin of promote_next_queued — called from the Celery teardown/TTL path."""
        booking = session.execute(_oldest_queued_stmt(resource_type)).scalar_one_or_none()
        if booking is None:
            return None
        resource = session.execute(_free_resource_stmt(resource_type)).scalar_one_or_none()
        if resource is None:
            return None
        _assign_resource_and_ready(session, booking, resource_type, resource)
        session.commit()
        return _to_entity(booking)
