from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import cast, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.database.models import BookingAuditModel, BookingModel, UserModel


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


def _to_entity(m: BookingModel, owner_username: str | None = None) -> Booking:
    return Booking(
        id=m.id,
        user_id=m.user_id,
        status=BookingStatus(m.status),
        ttl_minutes=m.ttl_minutes,
        expires_at=m.expires_at,
        created_at=m.created_at,
        image_id=m.image_id,
        image_name=m.image_name,
        hw_config_id=m.hw_config_id,
        hw_config_name=m.hw_config_name,
        vm_ip=m.vm_ip,
        owner_username=owner_username,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        hdd_mb=m.hdd_mb,
    )


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
            cpus=booking.cpus,
            memory_mb=booking.memory_mb,
            hdd_mb=booking.hdd_mb,
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
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        return _to_entity(model)

    async def update_status(
        self,
        session: AsyncSession,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
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
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
        await session.commit()

    async def list_all(self, session: AsyncSession) -> list[Booking]:
        result = await session.execute(
            select(BookingModel, UserModel.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .order_by(BookingModel.created_at.desc())
        )
        return [_to_entity(m, username) for m, username in result.all()]

    async def list_audit(self, session: AsyncSession, booking_id: UUID) -> list[BookingAuditEntry]:
        result = await session.execute(
            select(BookingAuditModel)
            .where(BookingAuditModel.booking_id == booking_id)
            .order_by(BookingAuditModel.created_at)
        )
        return [_to_audit_entity(m) for m in result.scalars().all()]

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
        actor_id: str = "system",
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_status = model.status
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
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
        """Return PENDING/PROVISIONING/RETRY bookings created more than threshold_minutes ago."""
        stale_statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
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
        """Return all PENDING/PROVISIONING/RETRY bookings regardless of age."""
        statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
            BookingStatus.RETRY.value,
        ]
        result = session.execute(
            select(BookingModel).where(BookingModel.status.in_(statuses))
        )
        return [_to_entity(m) for m in result.scalars().all()]
