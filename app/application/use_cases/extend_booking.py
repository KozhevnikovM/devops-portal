from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingError, BookingNotFoundError, PermissionError
from app.infrastructure.repositories.booking_repo import BookingRepository


class ExtendBookingUseCase:
    def __init__(self, repo: BookingRepository) -> None:
        self._repo = repo

    async def execute(
        self,
        session: AsyncSession,
        booking_id: UUID,
        extend_minutes: int,
        current_user: User,
    ) -> Booking:
        booking = await self._repo.get(session, booking_id)
        if booking.status != BookingStatus.READY:
            raise BookingError("can only extend READY bookings")
        if booking.ttl_minutes == 0:
            raise BookingError("cannot extend a permanent booking")
        if booking.user_id != str(current_user.id):
            raise PermissionError("only the owner can extend a booking")
        await self._repo.extend(session, booking_id, extend_minutes, actor_id=str(current_user.id))
        return await self._repo.get(session, booking_id)
