from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.application.ports import BookingRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.exceptions import BookingError, BookingNotFoundError, BookingPermissionError


class ExtendBookingUseCase:
    def __init__(self, repo: BookingRepositoryPort) -> None:
        self._repo = repo

    async def execute(
        self,
        session: AsyncSession,
        booking_id: UUID,
        extend_minutes: int,
        current_user: User,
    ) -> Booking:
        booking = await self._repo.get(session, booking_id)
        # Authorization first — anyone without management rights must get 403 regardless of the
        # booking's status/TTL, so we never leak state about a booking they can't manage. Owner,
        # the creating dispatcher (#229) and admins may extend.
        if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
            raise BookingPermissionError("not allowed to extend this booking")
        if booking.status != BookingStatus.READY:
            raise BookingError("can only extend READY bookings")
        if booking.ttl_minutes == 0:
            raise BookingError("cannot extend a permanent booking")
        await self._repo.extend(session, booking_id, extend_minutes, actor_id=str(current_user.id))
        return await self._repo.get(session, booking_id)
