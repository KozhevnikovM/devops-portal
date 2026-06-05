from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingError, BookingPermissionError
from app.infrastructure.celery_dispatcher import CeleryTaskDispatcher
from app.infrastructure.repositories.booking_repo import BookingRepository

# A booking the owner may release directly (it holds a live resource).
_RELEASABLE_STATUSES = {BookingStatus.READY, BookingStatus.FAILED}
# In-flight states an admin may force-delete (provisioning never reached a steady resource).
_FORCE_DELETABLE_STATUSES = {BookingStatus.PENDING, BookingStatus.PROVISIONING, BookingStatus.RETRY}
# States that are not safe for an ordinary release (work is mid-flight).
_IN_FLIGHT_STATUSES = {*_FORCE_DELETABLE_STATUSES, BookingStatus.RELEASING}


class ReleaseBookingUseCase:
    """Release (or cancel) a booking, enforcing ownership and the status machine.

    Shared by the browser (HTMX) and the JSON API so both honour exactly the same rules:
    owners release their own READY/FAILED bookings, admins may force-delete in-flight ones,
    QUEUED slots are cancelled, pooled resources go back to the pool (promoting the next queued
    booking) and provisioned VMs are torn down asynchronously.
    """

    def __init__(self, repo: BookingRepository, dispatcher: CeleryTaskDispatcher) -> None:
        self._repo = repo
        self._dispatcher = dispatcher

    async def execute(self, session: AsyncSession, booking_id: UUID, current_user: User) -> Booking:
        booking = await self._repo.get(session, booking_id)  # raises BookingNotFoundError

        if booking.user_id != str(current_user.id) and current_user.role != "admin":
            raise BookingPermissionError("Not the booking owner")

        is_admin_force_delete = (
            current_user.role == "admin" and booking.status in _FORCE_DELETABLE_STATUSES
        )

        if booking.status == BookingStatus.QUEUED:
            # Cancel the queue slot — holds no resource, so nothing to tear down or promote.
            await self._repo.update_status(
                session, booking_id, BookingStatus.RELEASED, actor_id=str(current_user.id)
            )
        else:
            if not is_admin_force_delete:
                if booking.status in _IN_FLIGHT_STATUSES:
                    raise BookingError("Cannot release an in-flight booking")
                if booking.status not in _RELEASABLE_STATUSES:
                    raise BookingError(f"Cannot release booking with status {booking.status.value}")

            if booking.resource_type in (ResourceType.NAMESPACE, ResourceType.STATIC_VM):
                # Pooled resource — return it to the pool, then hand it to the next queued booking.
                await self._repo.update_status(
                    session, booking_id, BookingStatus.RELEASED, actor_id=str(current_user.id)
                )
                await self._repo.promote_next_queued(session, booking.resource_type.value)
            else:
                await self._repo.update_status(
                    session, booking_id, BookingStatus.RELEASING, actor_id=str(current_user.id)
                )
                self._dispatcher.dispatch_teardown(str(booking_id))

        return await self._repo.get(session, booking_id)
