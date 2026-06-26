"""ShareNamespaceUseCase — grant another portal user read-only access to a namespace booking."""
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import BookingRepositoryPort, NamespaceShareRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.entities import NamespaceShare, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingNotFoundError,
    BookingPermissionError,
    NamespaceShareDuplicateError,
    NamespaceShareSelfError,
    NamespaceShareUserNotFoundError,
)

_TERMINAL = {BookingStatus.RELEASED, BookingStatus.FAILED}


class ShareNamespaceUseCase:
    """Grant user `shared_with_username` read-only access to booking `booking_id`.

    Caller must be the owner, the creating dispatcher, or an admin.
    The target booking must be a live NAMESPACE booking (not RELEASED or FAILED).
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
    ) -> NamespaceShare:
        # Load and validate the booking.
        try:
            booking = await self._booking_repo.get(session, booking_id)
        except (ValueError, BookingNotFoundError):
            raise BookingNotFoundError(f"Booking {booking_id} not found")

        if booking.resource_type != ResourceType.NAMESPACE:
            raise ValueError("Only NAMESPACE bookings can be shared")

        if booking.status in _TERMINAL:
            raise ValueError(f"Cannot share a {booking.status.value} booking")

        # Authorization check.
        if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=caller):
            raise BookingPermissionError("Not authorized to share this booking")

        # Resolve the recipient username → user.  We import the UserRepository directly here
        # because there is no port for user lookups yet; the use case still satisfies the
        # dependency-direction rule (infrastructure → application is fine from the composition root,
        # but we need to look up a user *inside* the use case).  To stay pure we accept the user_id
        # from the caller's resolved entity instead.
        #
        # Practical approach: re-use the session to query UserModel directly via the repo.
        from app.infrastructure.repositories.user_repo import UserRepository  # noqa: PLC0415
        _user_repo = UserRepository()
        recipient = await _user_repo.get_by_username(session, shared_with_username)

        if recipient is None or not recipient.is_active:
            raise NamespaceShareUserNotFoundError(
                f"User '{shared_with_username}' not found or inactive"
            )

        # Prevent self-share.
        if recipient.id == caller.id:
            raise NamespaceShareSelfError("Cannot share a namespace with yourself")

        # Create the share (flush to catch unique violation before commit).
        try:
            share = await self._share_repo.create(session, booking_id, recipient.id)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise NamespaceShareDuplicateError(
                f"Booking {booking_id} is already shared with '{shared_with_username}'"
            )

        return share
