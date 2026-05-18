import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingError, BookingNotFoundError, PermissionError
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.infrastructure.repositories.booking_repo import BookingRepository


def _make_user(**kwargs) -> User:
    return User(
        id=kwargs.get("id", uuid4()),
        username=kwargs.get("username", "alice"),
        password_hash="x",
        role=kwargs.get("role", "user"),
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _make_booking(user_id: str, status: BookingStatus = BookingStatus.READY, ttl_minutes: int = 240) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=status,
        ttl_minutes=ttl_minutes,
        expires_at=now + timedelta(minutes=ttl_minutes) if ttl_minutes else datetime(9999, 12, 31, tzinfo=timezone.utc),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


@pytest.fixture
def owner():
    return _make_user(username="alice")


@pytest.fixture
def other_user():
    return _make_user(username="bob")


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_repo(booking: Booking) -> BookingRepository:
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.extend = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_extend_advances_ttl_and_expiry(owner, mock_session):
    booking = _make_booking(str(owner.id), ttl_minutes=120)
    original_expires_at = booking.expires_at
    extended_booking = Booking(
        **{**booking.__dict__, "ttl_minutes": 180, "expires_at": original_expires_at + timedelta(minutes=60)}
    )
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(side_effect=[booking, extended_booking])
    repo.extend = AsyncMock()
    use_case = ExtendBookingUseCase(repo)

    result = await use_case.execute(mock_session, booking.id, extend_minutes=60, current_user=owner)

    repo.extend.assert_called_once_with(mock_session, booking.id, 60, actor_id=str(owner.id))
    assert result.ttl_minutes == 180
    assert result.expires_at == original_expires_at + timedelta(minutes=60)


@pytest.mark.asyncio
async def test_extend_non_ready_booking_raises_409(owner, mock_session):
    booking = _make_booking(str(owner.id), status=BookingStatus.PROVISIONING)
    repo = _make_repo(booking)
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(BookingError, match="READY"):
        await use_case.execute(mock_session, booking.id, extend_minutes=60, current_user=owner)

    repo.extend.assert_not_called()


@pytest.mark.asyncio
async def test_extend_permanent_booking_raises_409(owner, mock_session):
    booking = _make_booking(str(owner.id), ttl_minutes=0)
    repo = _make_repo(booking)
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(BookingError, match="permanent"):
        await use_case.execute(mock_session, booking.id, extend_minutes=60, current_user=owner)

    repo.extend.assert_not_called()


@pytest.mark.asyncio
async def test_extend_wrong_owner_raises_403(other_user, mock_session):
    booking = _make_booking(str(uuid4()))  # owned by someone else
    repo = _make_repo(booking)
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(PermissionError):
        await use_case.execute(mock_session, booking.id, extend_minutes=60, current_user=other_user)

    repo.extend.assert_not_called()


@pytest.mark.asyncio
async def test_extend_missing_booking_raises_404(owner, mock_session):
    booking_id = uuid4()
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(side_effect=BookingNotFoundError(booking_id))
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(BookingNotFoundError):
        await use_case.execute(mock_session, booking_id, extend_minutes=60, current_user=owner)


@pytest.mark.asyncio
async def test_extend_to_forever_clears_ttl(owner, mock_session):
    booking = _make_booking(str(owner.id), ttl_minutes=120)
    permanent_booking = Booking(
        **{**booking.__dict__, "ttl_minutes": 0, "expires_at": datetime(9999, 12, 31, tzinfo=timezone.utc)}
    )
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(side_effect=[booking, permanent_booking])
    repo.extend = AsyncMock()
    use_case = ExtendBookingUseCase(repo)

    result = await use_case.execute(mock_session, booking.id, extend_minutes=0, current_user=owner)

    repo.extend.assert_called_once_with(mock_session, booking.id, 0, actor_id=str(owner.id))
    assert result.ttl_minutes == 0


@pytest.mark.asyncio
async def test_extend_failed_booking_raises_409(owner, mock_session):
    booking = _make_booking(str(owner.id), status=BookingStatus.FAILED)
    repo = _make_repo(booking)
    use_case = ExtendBookingUseCase(repo)

    with pytest.raises(BookingError, match="READY"):
        await use_case.execute(mock_session, booking.id, extend_minutes=60, current_user=owner)
