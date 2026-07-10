"""Regression tests for admin force-release of failed VM bookings (#278).

Covers:
- teardown_vm_task with force=True ignores terraform errors and marks RELEASED
- TerraformVcdAdapter._destroy_state with force=True logs warning instead of raising
- Admin endpoint rejects non-FAILED and non-VM bookings; dispatches force teardown on FAILED VM
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.terraform.vcd_adapter import TerraformError, TerraformVcdAdapter


# ── teardown_vm_task force mode ───────────────────────────────────────────────

def _run_force_teardown(booking_id: str) -> MagicMock:
    """Run teardown_vm_task(force=True) with a terraform.destroy that always raises."""
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=SimpleNamespace(
        resource_type=ResourceType.VM,
        image_id=str(uuid4()),
        hw_config_id=str(uuid4()),
        vm_password="pw",
    ))
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(
        return_value=MagicMock(vapp_template_id="t")
    )
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(
        return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=26624)
    )

    async def failing_destroy(workspace_id, config, api_token=None, on_progress=None, force=False):
        raise Exception("API Error: 400: vApp is not running")

    mock_terraform = MagicMock()
    mock_terraform.destroy = failing_destroy

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_sf,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.terraform", mock_terraform),
        patch("app.tasks.teardown.settings") as s,
    ):
        s.USE_STUB_TERRAFORM = False
        s.VCD_API_TOKENS = ""
        s.VCD_API_TOKEN = "tok"
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id], kwargs={"force": True})

    return mock_repo


def test_force_teardown_marks_released_despite_terraform_error():
    mock_repo = _run_force_teardown(str(uuid4()))
    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASED in statuses


def test_force_teardown_does_not_set_failed():
    mock_repo = _run_force_teardown(str(uuid4()))
    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.FAILED not in statuses


# ── VcdAdapter._destroy_state force mode ─────────────────────────────────────

@pytest.mark.asyncio
async def test_destroy_state_force_returns_on_error(tmp_path):
    adapter = TerraformVcdAdapter()

    async def _failing_run(*args, **kwargs):
        raise TerraformError("API Error: 400: vApp is not running")

    adapter._run = _failing_run
    # Should not raise:
    await adapter._destroy_state("ws-1", tmp_path, force=True)


@pytest.mark.asyncio
async def test_destroy_state_no_force_raises(tmp_path):
    adapter = TerraformVcdAdapter()

    async def _failing_run(*args, **kwargs):
        raise TerraformError("API Error: 400: vApp is not running")

    adapter._run = _failing_run
    with pytest.raises(TerraformError):
        await adapter._destroy_state("ws-1", tmp_path, force=False)


# ── Admin force-release endpoint ─────────────────────────────────────────────

@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin
    from fastapi.testclient import TestClient
    admin_user = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_admin] = lambda: admin_user
    yield TestClient(app), admin_user
    app.dependency_overrides.clear()


def _failed_vm_booking():
    from datetime import datetime, timedelta, timezone
    from app.domain.entities import Booking
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.FAILED, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


def _releasing_vm_booking():
    from datetime import datetime, timedelta, timezone
    from app.domain.entities import Booking
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.RELEASING, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


def test_admin_force_release_dispatches_and_returns_202(admin_client):
    client, _ = admin_client
    booking = _failed_vm_booking()
    releasing = _failed_vm_booking()
    releasing = type(booking)(
        id=booking.id, user_id=booking.user_id, status=BookingStatus.RELEASING,
        resource_type=booking.resource_type, ttl_minutes=booking.ttl_minutes,
        expires_at=booking.expires_at, created_at=booking.created_at,
        image_id=booking.image_id, image_name=booking.image_name,
        hw_config_id=booking.hw_config_id, hw_config_name=booking.hw_config_name,
    )
    with patch("app.presentation.routes.admin._booking_repo") as repo, \
         patch("app.presentation.routes.admin.dispatcher") as disp:
        repo.get = AsyncMock(side_effect=[booking, releasing])
        repo.update_status = AsyncMock()
        resp = client.post(f"/admin/bookings/{booking.id}/force-release")
    assert resp.status_code == 202
    disp.dispatch_teardown_force.assert_called_once_with(str(booking.id))


def test_admin_force_release_rejects_non_failed(admin_client):
    client, _ = admin_client
    booking = _releasing_vm_booking()
    with patch("app.presentation.routes.admin._booking_repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.post(f"/admin/bookings/{booking.id}/force-release")
    assert resp.status_code == 400


def test_admin_force_release_rejects_non_vm(admin_client):
    from datetime import datetime, timedelta, timezone
    from app.domain.entities import Booking
    client, _ = admin_client
    now = datetime.now(timezone.utc)
    ns_booking = Booking(
        id=uuid4(), user_id="u", status=BookingStatus.FAILED, resource_type=ResourceType.NAMESPACE,
        ttl_minutes=0, expires_at=now + timedelta(minutes=1), created_at=now,
    )
    with patch("app.presentation.routes.admin._booking_repo") as repo:
        repo.get = AsyncMock(return_value=ns_booking)
        resp = client.post(f"/admin/bookings/{ns_booking.id}/force-release")
    assert resp.status_code == 400
