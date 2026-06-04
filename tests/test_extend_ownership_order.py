"""Regression test for #141 — ownership is checked before status/TTL in extend.

Before the fix, a non-owner extending a non-READY or permanent booking got a 409
("can only extend READY bookings" / "cannot extend a permanent booking"), leaking the
booking's state. After the fix the ownership check runs first → always 403.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingPermissionError
from app.infrastructure.repositories.booking_repo import BookingRepository


def _user() -> User:
    return User(
        id=uuid4(), username="bob", password_hash="x", role="user",
        is_active=True, created_at=datetime.now(timezone.utc),
    )


def _booking(status: BookingStatus, ttl_minutes: int) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=str(uuid4()),  # owned by someone else
        status=status,
        ttl_minutes=ttl_minutes,
        expires_at=now + timedelta(minutes=ttl_minutes or 1),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


def _repo(booking: Booking) -> BookingRepository:
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.extend = AsyncMock()
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,ttl",
    [
        (BookingStatus.PROVISIONING, 240),  # not READY
        (BookingStatus.FAILED, 240),        # not READY
        (BookingStatus.READY, 0),           # permanent
    ],
)
async def test_non_owner_gets_403_regardless_of_state(status, ttl):
    other = _user()
    booking = _booking(status, ttl)
    repo = _repo(booking)
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(BookingPermissionError):
        await use_case.execute(AsyncMock(), booking.id, extend_minutes=60, current_user=other)

    repo.extend.assert_not_called()
