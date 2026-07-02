"""RevokeNamespaceShareUseCase — remove a namespace share."""
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import BookingRepositoryPort, NamespaceShareRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.entities import User
from app.domain.exceptions import (
    BookingNotFoundError,
    BookingPermissionError,
    NamespaceShareNotFoundError,
    NamespaceShareUserNotFoundError,
)


class RevokeNamespaceShareUseCase:
    """Revoke an existing namespace share.

    Caller must be the owner, the creating dispatcher, or an admin.
    """

    def __init__(
        self,
        booking_repo: BookingRepositoryPort,
        share_repo: NamespaceShareRepositoryPort,
    ) -> None:
        self._booking_repo = booking_repo
        self._share_repo = share_repo

    async def execute(
        self,
        session: AsyncSession,
        booking_id: UUID,
        shared_with_username: str,
        caller: User,
    ) -> None:
        # Load and validate the booking.
        try:
            booking = await self._booking_repo.get(session, booking_id)
        except (ValueError, BookingNotFoundError):
            raise BookingNotFoundError(f"Booking {booking_id} not found")

        # Authorization check.
        if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=caller):
            raise BookingPermissionError("Not authorized to revoke shares for this booking")

        # Resolve the username.
        from app.infrastructure.repositories.user_repo import UserRepository  # noqa: PLC0415
        _user_repo = UserRepository()
        recipient = await _user_repo.get_by_username(session, shared_with_username)

        if recipient is None:
            raise NamespaceShareUserNotFoundError(f"User '{shared_with_username}' not found")

        deleted = await self._share_repo.delete(session, booking_id, recipient.id)
        if not deleted:
            raise NamespaceShareNotFoundError(
                f"No share found for booking {booking_id} and user '{shared_with_username}'"
            )

        await session.commit()
