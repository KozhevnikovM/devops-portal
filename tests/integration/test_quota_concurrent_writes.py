"""Integration: quota ceiling is enforced under concurrent writes with SELECT FOR UPDATE.

Two concurrent CreateBookingUseCase.execute() calls for the same user, using separate
database sessions, race to claim the last quota slot.  The SELECT FOR UPDATE in
QuotaRepository.get_limits_for_update() serialises the two sessions so only one booking
is accepted and the other raises QuotaExceededError.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.domain.exceptions import QuotaExceededError
from app.infrastructure.database.models import BookingAuditModel, BookingModel, QuotaModel
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.quota_repo import QuotaRepository

pytestmark = pytest.mark.integration

_HW_CPUS = 3  # > half of the tight quota below so two together exceed the limit
_QUOTA_MAX_CPUS = 4


def _make_uc() -> CreateBookingUseCase:
    return CreateBookingUseCase(
        repo=BookingRepository(),
        image_repo=ImageRepository(),
        hw_config_repo=HWConfigRepository(),
        quota_repo=QuotaRepository(),
        dispatcher=MagicMock(),
    )


async def _try_create(engine: AsyncEngine, uc, user_id, image_id, hw_id):
    """Create one booking in its own session (which commits on success)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await uc.execute(
            session, 60, image_id, hw_id, user_id=user_id, dispatch=False
        )


@pytest.mark.asyncio
async def test_quota_ceiling_enforced_under_concurrent_writes(
    async_engine: AsyncEngine, seed_catalog: dict
):
    """Only one of two concurrent bookings is accepted when the second would exceed quota."""
    user_id = str(uuid4())
    image_id = seed_catalog["image_id"]
    hw_id = seed_catalog["hw_id"]
    uc = _make_uc()

    # Seed a tight quota for the test user (max_cpus=4; each booking uses 3 CPUs).
    quota_repo = QuotaRepository()
    async with AsyncSession(async_engine) as session:
        await quota_repo.set(
            session, UUID(user_id),
            max_cpus=_QUOTA_MAX_CPUS, max_memory_gb=32, max_ssd_gb=500, max_hdd_gb=500,
        )

    results = await asyncio.gather(
        _try_create(async_engine, uc, user_id, image_id, hw_id),
        _try_create(async_engine, uc, user_id, image_id, hw_id),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures  = [r for r in results if isinstance(r, QuotaExceededError)]
    errors    = [r for r in results if isinstance(r, Exception) and not isinstance(r, QuotaExceededError)]

    assert errors == [], f"Unexpected exceptions: {errors}"
    assert len(successes) == 1, f"Expected exactly one booking, got: {successes}"
    assert len(failures)  == 1, "Expected exactly one QuotaExceededError"

    # Cleanup committed rows so later tests/sessions are not affected.
    async with AsyncSession(async_engine) as session:
        await session.execute(
            delete(BookingAuditModel).where(
                BookingAuditModel.booking_id.in_(
                    [b.id for b in successes]
                )
            )
        )
        await session.execute(
            delete(BookingModel).where(BookingModel.user_id == user_id)
        )
        await session.execute(
            delete(QuotaModel).where(QuotaModel.user_id == UUID(user_id))
        )
        await session.commit()
