from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.application.ports import BookingRepositoryPort, TaskDispatcher
from app.application.use_cases._permissions import can_manage
from app.domain.exceptions import BookingError, BookingPermissionError

# A booking the owner may release directly (it holds a live resource).
_RELEASABLE_STATUSES = {BookingStatus.READY, BookingStatus.FAILED}
# In-flight states an admin may force-delete (provisioning never reached a steady resource).
# CONFIGURING included: the VM exists in VCD, so an abandoned config still tears it down.
_FORCE_DELETABLE_STATUSES = {
    BookingStatus.PENDING, BookingStatus.PROVISIONING, BookingStatus.CONFIGURING, BookingStatus.RETRY,
}
# States that are not safe for an ordinary release (work is mid-flight).
_IN_FLIGHT_STATUSES = {*_FORCE_DELETABLE_STATUSES, BookingStatus.RELEASING}


class ReleaseBookingUseCase:
    """Release (or cancel) a booking, enforcing ownership and the status machine.

    Shared by the browser (HTMX) and the JSON API so both honour exactly the same rules:
    owners release their own READY/FAILED bookings, admins may force-delete in-flight ones,
    QUEUED slots are cancelled, pooled resources go back to the pool (promoting the next queued
    booking) and provisioned VMs are torn down asynchronously.
    """

    def __init__(self, repo: BookingRepositoryPort, dispatcher: TaskDispatcher) -> None:
        self._repo = repo
        self._dispatcher = dispatcher

    async def execute(
        self, session: AsyncSession, booking_id: UUID, current_user: User, force: bool = False,
    ) -> Booking:
        booking = await self._repo.get(session, booking_id)  # raises BookingNotFoundError

        if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
            raise BookingPermissionError("Not the booking owner")

        # Prevent releasing individual bookings that are actively held inside an environment
        # (READY / in-flight / queued). FAILED children may be released directly because
        # ReleaseEnvironmentUseCase skips them (treats them as terminal already), leaving them
        # stranded with environment_id set but no live resource to protect.
        # force=True bypasses this entirely (used by ReleaseEnvironmentUseCase itself).
        _ENV_TERMINAL = {BookingStatus.RELEASED, BookingStatus.FAILED}
        if booking.environment_id is not None and not force and booking.status not in _ENV_TERMINAL:
            raise BookingError("This booking belongs to an environment — release the environment instead")

        # `force` (used when releasing a whole environment) tears down any non-terminal child
        # regardless of status — the same effect as an admin force-delete.
        is_admin_force_delete = force or (
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
