from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError  # noqa: F401 – re-exported for callers


class ForceReleaseBookingUseCase:
    """Admin-only: force a FAILED or stuck-RELEASING VM booking to its final state.

    Both branches ensure the booking is RELEASING, then (re-)dispatch forced teardown.
    Safe to re-dispatch on an already-RELEASING booking (#374): the status guard treats
    RELEASING→RELEASING as a no-op, and force=True lands on RELEASED even if the underlying
    VM/vApp is already gone (e.g. removed out-of-band, or the original teardown finished
    destroying but never got to update the status before the process died).
    """

    def __init__(self, repo, dispatcher) -> None:
        self._repo = repo
        self._dispatcher = dispatcher

    async def execute(self, session: AsyncSession, booking_id: UUID, actor_id: str) -> Booking:
        booking = await self._repo.get(session, booking_id)  # raises BookingNotFoundError

        if booking.resource_type != ResourceType.VM:
            raise ValueError("Force release is only available for VM bookings")
        if booking.status not in {BookingStatus.FAILED, BookingStatus.RELEASING}:
            raise ValueError(
                f"Booking is {booking.status.value}; must be FAILED or RELEASING to force-release"
            )

        if booking.status != BookingStatus.RELEASING:
            await self._repo.update_status(
                session, booking_id, BookingStatus.RELEASING, actor_id=actor_id
            )
        self._dispatcher.dispatch_teardown_force(str(booking_id))

        return await self._repo.get(session, booking_id)
