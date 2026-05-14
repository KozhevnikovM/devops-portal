import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from app.domain.entities import Booking, VMImage, HWConfig
from app.domain.enums import BookingStatus
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository


def _make_image(**kwargs) -> VMImage:
    return VMImage(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "Ubuntu 22.04"),
        vapp_template_id=kwargs.get("vapp_template_id", "tpl-001"),
        is_active=kwargs.get("is_active", True),
        created_at=kwargs.get("created_at", datetime.now(timezone.utc)),
    )


def _make_hw(**kwargs) -> HWConfig:
    return HWConfig(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "medium"),
        cpus=kwargs.get("cpus", 2),
        memory_mb=kwargs.get("memory_mb", 4096),
        disk_mb=kwargs.get("disk_mb", 26624),
        is_active=kwargs.get("is_active", True),
        created_at=kwargs.get("created_at", datetime.now(timezone.utc)),
    )


@pytest.fixture
def mock_image():
    return _make_image()


@pytest.fixture
def mock_hw():
    return _make_hw()


@pytest.fixture
def mock_repo():
    repo = MagicMock(spec=BookingRepository)
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    return repo


@pytest.fixture
def mock_image_repo(mock_image):
    repo = MagicMock(spec=ImageRepository)
    repo.get = AsyncMock(return_value=mock_image)
    return repo


@pytest.fixture
def mock_hw_repo(mock_hw):
    repo = MagicMock(spec=HWConfigRepository)
    repo.get = AsyncMock(return_value=mock_hw)
    return repo


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_create_booking_returns_pending(mock_repo, mock_image_repo, mock_hw_repo, mock_image, mock_hw, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_image_repo, mock_hw_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=4, image_id=mock_image.id, hw_config_id=mock_hw.id)

    assert booking.status == BookingStatus.PENDING
    assert booking.ttl_hours == 4
    assert booking.vm_ip is None


@pytest.mark.asyncio
async def test_create_booking_sets_image_and_hw_fields(mock_repo, mock_image_repo, mock_hw_repo, mock_image, mock_hw, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_image_repo, mock_hw_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=4, image_id=mock_image.id, hw_config_id=mock_hw.id)

    assert booking.image_id == mock_image.id
    assert booking.image_name == mock_image.name
    assert booking.hw_config_id == mock_hw.id
    assert booking.hw_config_name == mock_hw.name


@pytest.mark.asyncio
async def test_create_booking_dispatches_task(mock_repo, mock_image_repo, mock_hw_repo, mock_image, mock_hw, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_image_repo, mock_hw_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=1, image_id=mock_image.id, hw_config_id=mock_hw.id)
        mock_task.delay.assert_called_once_with(str(booking.id), str(mock_image.id), str(mock_hw.id))


@pytest.mark.asyncio
async def test_create_booking_sets_correct_expiry(mock_repo, mock_image_repo, mock_hw_repo, mock_image, mock_hw, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_image_repo, mock_hw_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=8, image_id=mock_image.id, hw_config_id=mock_hw.id)

    delta = booking.expires_at - booking.created_at
    assert abs(delta.total_seconds() - 8 * 3600) < 2


@pytest.mark.asyncio
async def test_create_booking_raises_for_inactive_image(mock_repo, mock_hw_repo, mock_hw, mock_session):
    image_repo = MagicMock(spec=ImageRepository)
    image_repo.get = AsyncMock(side_effect=ValueError("inactive"))
    use_case = CreateBookingUseCase(mock_repo, image_repo, mock_hw_repo)

    with pytest.raises(ValueError):
        await use_case.execute(mock_session, ttl_hours=4, image_id=uuid4(), hw_config_id=mock_hw.id)


@pytest.mark.asyncio
async def test_create_booking_raises_for_inactive_hw(mock_repo, mock_image_repo, mock_image, mock_session):
    hw_repo = MagicMock(spec=HWConfigRepository)
    hw_repo.get = AsyncMock(side_effect=ValueError("inactive"))
    use_case = CreateBookingUseCase(mock_repo, mock_image_repo, hw_repo)

    with pytest.raises(ValueError):
        await use_case.execute(mock_session, ttl_hours=4, image_id=mock_image.id, hw_config_id=uuid4())
