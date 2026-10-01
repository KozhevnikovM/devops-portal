"""Queue positions for rendered booking rows (#495) — the one rank path every surface shares.

The list pages, the single-row refresh, the create/label responses and the live row update all go
through `attach_queue_positions`, so a list row and its refreshed row cannot disagree. Responses
for actions that cannot leave a booking QUEUED (extend, release, force-release) don't need it.
"""
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import BookingRepositoryPort
from app.domain.booking_list import BookingListItem
from app.domain.entities import Booking
from app.domain.enums import BookingStatus


async def attach_queue_positions(
    session: AsyncSession, repo: BookingRepositoryPort, bookings: Sequence[Booking | BookingListItem],
) -> None:
    """Set `queue_position` on the QUEUED bookings, read in one statement (none if no QUEUED).

    A booking promoted or released since it was read gets no position (rendered "—").
    """
    queued = [b for b in bookings if b.status == BookingStatus.QUEUED]
    if not queued:
        return
    positions = await repo.queue_positions(session, queued)
    for b in queued:
        b.queue_position = positions.get(b.id)
