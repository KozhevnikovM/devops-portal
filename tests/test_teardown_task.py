import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.domain.enums import BookingStatus


def _mock_booking(booking_id):
    b = MagicMock()
    b.image_id = uuid4()
    b.hw_config_id = uuid4()
    return b


def _mock_image(vapp_template_id="tpl-001"):
    img = MagicMock()
    img.vapp_template_id = vapp_template_id
    return img


def _mock_hw(cpus=2, memory_mb=4096, hdd_mb=26624):
    hw = MagicMock()
    hw.cpus = cpus
    hw.memory_mb = memory_mb
    hw.hdd_mb = hdd_mb
    return hw


def _patched(booking_id, extra_patches=()):
    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=_mock_booking(booking_id))
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_mock_image())
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_mock_hw())
    return mock_session, mock_repo, mock_image_repo, mock_hw_repo


def test_teardown_task_sets_released_status():
    """Teardown task transitions RELEASING → RELEASED on success."""
    booking_id = str(uuid4())
    mock_session, mock_repo, mock_image_repo, mock_hw_repo = _patched(booking_id)

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.asyncio.run", return_value=None),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASING in statuses
    assert BookingStatus.RELEASED in statuses


def test_teardown_task_sets_failed_on_final_retry():
    """After all retries are exhausted, teardown sets FAILED."""
    booking_id = str(uuid4())
    mock_session, mock_repo, mock_image_repo, mock_hw_repo = _patched(booking_id)

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.asyncio.run", side_effect=RuntimeError("destroy failed")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASING in statuses
    assert BookingStatus.FAILED in statuses


def test_teardown_task_calls_destroy_with_workspace_id_and_config():
    """Teardown passes workspace ID and reconstructed config to terraform.destroy."""
    booking_id = str(uuid4())
    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_booking = _mock_booking(booking_id)
    mock_booking.vm_password = "TestPass1234abCD"
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock()
    mock_image = _mock_image(vapp_template_id="tpl-abc")
    mock_image_repo.sync_get = MagicMock(return_value=mock_image)
    mock_hw_repo = MagicMock()
    mock_hw = _mock_hw(cpus=4, memory_mb=8192, hdd_mb=51200)
    mock_hw_repo.sync_get = MagicMock(return_value=mock_hw)

    mock_terraform = MagicMock()
    mock_terraform.destroy = AsyncMock(return_value=None)

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.terraform", mock_terraform),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    expected_config = {
        "name":             f"portal-{booking_id[:8]}",
        "vapp_template_id": "tpl-abc",
        "cpus":             4,
        "memory":           8192,
        "disk_size":        51200,
        "vm_password":      "TestPass1234abCD",
    }
    call_args = mock_terraform.destroy.call_args
    assert call_args.args == (f"booking-{booking_id}", expected_config, None)
    assert callable(call_args.kwargs.get("on_progress"))


@pytest.mark.asyncio
async def test_stub_adapter_destroy_completes():
    """StubTerraformAdapter.destroy completes without error."""
    from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
    adapter = StubTerraformAdapter()
    with patch("asyncio.sleep", return_value=None):
        await adapter.destroy("booking-test-workspace", config={})
