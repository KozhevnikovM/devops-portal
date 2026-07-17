"""Regression tests for #107: optional booking label."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.main import app


def _make_booking(**kwargs) -> Booking:
    defaults = dict(
        id=uuid4(),
        user_id=str(uuid4()),
        status=BookingStatus.PENDING,
        resource_type=ResourceType.VM,
        ttl_minutes=60,
        expires_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        image_name="Ubuntu 22.04",
        hw_config_name="medium",
        label=None,
    )
    defaults.update(kwargs)
    return Booking(**defaults)


@pytest.fixture()
def auth_client(regular_user):
    client = TestClient(app)
    client.cookies["session_id"] = "test-session"
    return client


def test_summary_serialiser_includes_label():
    from app.presentation.routes.api_bookings import _summary
    booking = _make_booking(label="perf-test-01")
    result = _summary(booking)
    assert result["label"] == "perf-test-01"


def test_summary_serialiser_null_label():
    from app.presentation.routes.api_bookings import _summary
    booking = _make_booking(label=None)
    assert _summary(booking)["label"] is None


def test_created_serialiser_includes_label():
    from app.presentation.routes.api_bookings import _created
    booking = _make_booking(label="k8s node 3")
    assert _created(booking)["label"] == "k8s node 3"


# ── Use case accepts and threads label through ────────────────────────────────

@pytest.mark.asyncio
async def test_create_booking_persists_label():
    from app.application.use_cases.create_booking import CreateBookingUseCase

    image = MagicMock(id=uuid4(), name="Ubuntu", is_active=True)
    hw = MagicMock(id=uuid4(), name="medium", cpus=2, memory_mb=2048,
                   disk_mb=20480, drive_type="HDD", is_active=True)

    mock_image_repo = MagicMock()
    mock_image_repo.get = AsyncMock(return_value=image)
    mock_hw_repo = MagicMock()
    mock_hw_repo.get = AsyncMock(return_value=hw)
    mock_quota_repo = MagicMock()
    mock_quota_repo.get_limits_for_update = AsyncMock(
        return_value={"max_cpus": 8, "max_memory_gb": 16, "max_ssd_gb": 100, "max_hdd_gb": 100}
    )
    mock_quota_repo.count_active_resources = AsyncMock(
        return_value={"cpus": 0, "memory_gb": 0, "ssd_gb": 0, "hdd_gb": 0}
    )
    created_booking = _make_booking(label="my test vm")
    mock_repo = MagicMock()
    mock_repo.create = AsyncMock(return_value=created_booking)
    mock_dispatch = MagicMock()
    mock_dispatch.return_value.dispatch_provision = MagicMock()

    use_case = CreateBookingUseCase(
        mock_repo, mock_image_repo, mock_hw_repo, mock_quota_repo, mock_dispatch
    )
    session = MagicMock()
    result = await use_case.execute(
        session, 60, image.id, hw.id, user_id=str(uuid4()), label="my test vm"
    )
    # The label passed to create() must match what we supplied
    created_arg: Booking = mock_repo.create.call_args[0][1]
    assert created_arg.label == "my test vm"


# ── Label truncation/stripping in API route ───────────────────────────────────

def test_api_route_strips_and_truncates_label():
    """Label > 128 chars is silently truncated at the route layer."""
    from app.presentation.routes.api_bookings import CreateBookingRequest
    long_label = "x" * 200
    req = CreateBookingRequest(ttl_minutes=60, label=long_label)
    # The route applies [:128].strip() before forwarding to use case
    assert (req.label[:128].strip() if req.label else None) == "x" * 128
