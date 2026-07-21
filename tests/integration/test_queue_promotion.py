"""Integration: SELECT FOR UPDATE SKIP LOCKED prevents double-promotion of a queued slot.

Two concurrent promote_next_queued() calls race over a single QUEUED booking.
The SKIP LOCKED ensures only one session acquires the row-lock and promotes the
booking; the other sees an empty result set and returns None.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncio
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.database.models import BookingAuditModel, BookingModel
from app.infrastructure.repositories.booking_repo import BookingRepository

pytestmark = pytest.mark.integration


def _queued_ns_booking() -> BookingModel:
    now = datetime.now(timezone.utc)
    return BookingModel(
        id=uuid4(),
        user_id=f"inttest-queue-{uuid4()}",
        status=BookingStatus.QUEUED.value,
        resource_type=ResourceType.NAMESPACE.value,
        ttl_minutes=60,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        cpus=0,
        memory_mb=0,
        disk_mb=0,
    )


@pytest.mark.asyncio
async def test_no_double_promotion_under_concurrent_reads(
    async_engine: AsyncEngine, seed_catalog: dict
):
    """Only one of two concurrent promotes succeeds; the other returns None (SKIP LOCKED)."""
    booking_model = _queued_ns_booking()
    booking_id = booking_model.id

    # Commit the QUEUED booking so both concurrent sessions can see it.
    async with AsyncSession(async_engine) as session:
        session.add(booking_model)
        await session.commit()

    repo = BookingRepository()

    async def promote():
        async with AsyncSession(async_engine) as session:
            return await repo.promote_next_queued(session, ResourceType.NAMESPACE.value)

    results = await asyncio.gather(promote(), promote(), return_exceptions=True)

    errors    = [r for r in results if isinstance(r, Exception)]
    promoted  = [r for r in results if isinstance(r, Booking)]
    nones     = [r for r in results if r is None]

    assert errors == [], f"Unexpected exceptions: {errors}"
    assert len(promoted) == 1, f"Expected exactly one promotion, got {len(promoted)}"
    assert len(nones)    == 1, "Expected the second call to return None (SKIP LOCKED)"
    assert promoted[0].status == BookingStatus.READY
    assert promoted[0].id == booking_id

    # Cleanup — remove booking + audit rows committed during the test.
    async with AsyncSession(async_engine) as session:
        await session.execute(
            delete(BookingAuditModel).where(BookingAuditModel.booking_id == booking_id)
        )
        await session.execute(delete(BookingModel).where(BookingModel.id == booking_id))
        await session.commit()
