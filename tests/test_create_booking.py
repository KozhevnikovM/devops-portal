import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.infrastructure.repositories.booking_repo import BookingRepository


@pytest.fixture
def mock_repo():
    repo = MagicMock(spec=BookingRepository)
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    return repo


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_create_booking_returns_pending(mock_repo, mock_session):
    use_case = CreateBookingUseCase(mock_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=4)

    assert booking.status == BookingStatus.PENDING
    assert booking.ttl_hours == 4
    assert booking.vm_ip is None


@pytest.mark.asyncio
async def test_create_booking_dispatches_task(mock_repo, mock_session):
    use_case = CreateBookingUseCase(mock_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=1)
        mock_task.delay.assert_called_once_with(str(booking.id))


@pytest.mark.asyncio
async def test_create_booking_sets_correct_expiry(mock_repo, mock_session):
    use_case = CreateBookingUseCase(mock_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=8)

    delta = booking.expires_at - booking.created_at
    assert abs(delta.total_seconds() - 8 * 3600) < 2
