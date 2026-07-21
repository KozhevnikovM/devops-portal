from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import BookingRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.entities import Booking, User
from app.domain.exceptions import BookingError, BookingPermissionError

_MAX_LABEL_LENGTH = 128


class UpdateBookingLabelUseCase:
    """Rename a VM booking's display label. A label is a display annotation, not
    operational state, so it can be edited regardless of the booking's status."""

    def __init__(self, repo: BookingRepositoryPort) -> None:
        self._repo = repo

    async def execute(
        self, session: AsyncSession, booking_id: UUID, label: str | None, current_user: User,
    ) -> Booking:
        booking = await self._repo.get(session, booking_id)
        if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
            raise BookingPermissionError("not allowed to edit this booking")
        label = label.strip() if label else None
        if label and len(label) > _MAX_LABEL_LENGTH:
            raise BookingError(f"label must be at most {_MAX_LABEL_LENGTH} characters")
        await self._repo.update_label(session, booking_id, label or None, actor_id=str(current_user.id))
        return await self._repo.get(session, booking_id)
