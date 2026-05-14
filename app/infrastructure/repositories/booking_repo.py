from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.database.models import BookingModel


def _to_entity(m: BookingModel) -> Booking:
    return Booking(
        id=m.id,
        user_id=m.user_id,
        status=BookingStatus(m.status),
        ttl_hours=m.ttl_hours,
        expires_at=m.expires_at,
        created_at=m.created_at,
        image_id=m.image_id,
        image_name=m.image_name,
        hw_config_id=m.hw_config_id,
        hw_config_name=m.hw_config_name,
        vm_ip=m.vm_ip,
    )


class BookingRepository:
    async def create(self, session: AsyncSession, booking: Booking) -> Booking:
        model = BookingModel(
            id=booking.id,
            user_id=booking.user_id,
            status=booking.status.value,
            ttl_hours=booking.ttl_hours,
            expires_at=booking.expires_at,
            created_at=booking.created_at,
            image_id=booking.image_id,
            image_name=booking.image_name,
            hw_config_id=booking.hw_config_id,
            hw_config_name=booking.hw_config_name,
        )
        session.add(model)
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
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        await session.commit()

    async def list_all(self, session: AsyncSession) -> list[Booking]:
        result = await session.execute(select(BookingModel).order_by(BookingModel.created_at.desc()))
        return [_to_entity(m) for m in result.scalars().all()]

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
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        session.commit()
