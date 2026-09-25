"""The aggregate status of an environment, derived from its children (#434)."""
from collections.abc import Iterable

from app.domain.booking_status import CAN_BECOME_READY
from app.domain.enums import BookingStatus


def derive_environment_status(statuses: Iterable[BookingStatus]) -> BookingStatus:
    """Aggregate an environment's status from its children's statuses; the first matching rule wins.

    Anything the rules don't name — e.g. some children RELEASED/RELEASING while others are READY —
    is FAILED, so a partly released stack is never reported as a healthy READY.
    """
    statuses = list(statuses)
    if not statuses:
        return BookingStatus.READY
    if any(s == BookingStatus.FAILED for s in statuses):
        return BookingStatus.FAILED
    if any(s in CAN_BECOME_READY for s in statuses):
        return BookingStatus.PROVISIONING
    if all(s == BookingStatus.RELEASED for s in statuses):
        return BookingStatus.RELEASED
    if all(s == BookingStatus.READY for s in statuses):
        return BookingStatus.READY
    return BookingStatus.FAILED
