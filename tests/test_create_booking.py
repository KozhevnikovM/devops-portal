import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.domain.entities import Booking, VMTemplate
from app.domain.enums import BookingStatus
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.template_repo import TemplateRepository
from datetime import datetime, timezone


def _make_template(**kwargs) -> VMTemplate:
    defaults = {
        "id": uuid4(),
        "name": "Ubuntu 22.04",
        "vapp_template_id": "tpl-001",
        "cpus": 2,
        "memory_mb": 4096,
        "disk_mb": 26624,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }
    return VMTemplate(**{**defaults, **kwargs})


@pytest.fixture
def mock_template():
    return _make_template()


@pytest.fixture
def mock_repo(mock_template):
    repo = MagicMock(spec=BookingRepository)
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    return repo


@pytest.fixture
def mock_template_repo(mock_template):
    repo = MagicMock(spec=TemplateRepository)
    repo.get = AsyncMock(return_value=mock_template)
    return repo


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_create_booking_returns_pending(mock_repo, mock_template_repo, mock_template, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_template_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=4, template_id=mock_template.id)

    assert booking.status == BookingStatus.PENDING
    assert booking.ttl_hours == 4
    assert booking.vm_ip is None


@pytest.mark.asyncio
async def test_create_booking_sets_template_fields(mock_repo, mock_template_repo, mock_template, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_template_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=4, template_id=mock_template.id)

    assert booking.template_id == mock_template.id
    assert booking.template_name == mock_template.name


@pytest.mark.asyncio
async def test_create_booking_dispatches_task(mock_repo, mock_template_repo, mock_template, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_template_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=1, template_id=mock_template.id)
        mock_task.delay.assert_called_once_with(str(booking.id), str(mock_template.id))


@pytest.mark.asyncio
async def test_create_booking_sets_correct_expiry(mock_repo, mock_template_repo, mock_template, mock_session):
    use_case = CreateBookingUseCase(mock_repo, mock_template_repo)

    with patch("app.application.use_cases.create_booking.provision_vm_task") as mock_task:
        mock_task.delay = MagicMock()
        booking = await use_case.execute(mock_session, ttl_hours=8, template_id=mock_template.id)

    delta = booking.expires_at - booking.created_at
    assert abs(delta.total_seconds() - 8 * 3600) < 2


@pytest.mark.asyncio
async def test_create_booking_raises_for_inactive_template(mock_repo, mock_session):
    template_repo = MagicMock(spec=TemplateRepository)
    template_repo.get = AsyncMock(side_effect=ValueError("inactive"))
    use_case = CreateBookingUseCase(mock_repo, template_repo)

    with pytest.raises(ValueError):
        await use_case.execute(mock_session, ttl_hours=4, template_id=uuid4())
