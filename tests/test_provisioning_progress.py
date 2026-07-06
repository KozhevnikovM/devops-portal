from unittest.mock import MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

import pytest

from app.domain.entities import VMImage, HWConfig
from app.domain.enums import BookingStatus
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter


def _make_image():
    return VMImage(
        id=uuid4(), name="Ubuntu 22.04", vapp_template_id="tpl-001",
        is_active=True, created_at=datetime.now(timezone.utc),
    )


def _make_hw():
    return HWConfig(
        id=uuid4(), name="medium", cpus=2, memory_mb=4096, disk_mb=26624,
        is_active=True, created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Stub adapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stub_adapter_apply_calls_on_progress():
    adapter = StubTerraformAdapter()
    received = []
    with patch("asyncio.sleep", return_value=None):
        result = await adapter.apply("ws-1", {}, on_progress=received.append)
    assert "Provisioning (stub mode)…" in received
    assert "ip" in result


@pytest.mark.asyncio
async def test_stub_adapter_destroy_calls_on_progress():
    adapter = StubTerraformAdapter()
    received = []
    with patch("asyncio.sleep", return_value=None):
        await adapter.destroy("ws-1", {}, on_progress=received.append)
    assert "Destroying (stub mode)…" in received


@pytest.mark.asyncio
async def test_stub_adapter_apply_no_progress_callback():
    """on_progress=None must not raise."""
    adapter = StubTerraformAdapter()
    with patch("asyncio.sleep", return_value=None):
        result = await adapter.apply("ws-1", {})
    assert "ip" in result


# ---------------------------------------------------------------------------
# provision_vm_task — terraform object mocked so on_progress is invoked
# ---------------------------------------------------------------------------

def _run_provision(booking_id, image_id, hw_config_id, *, terraform_apply_side_effect=None, fake_ip="10.0.0.1"):
    """Helper: run provision_vm_task with a fake terraform whose apply calls on_progress."""
    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_make_image())
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_make_hw())

    async def fake_apply(workspace_id, config, api_token=None, on_progress=None):
        if terraform_apply_side_effect:
            raise terraform_apply_side_effect
        if on_progress:
            on_progress("Provisioning (stub mode)…")
        return {"ip": fake_ip}

    mock_terraform = MagicMock()
    mock_terraform.apply = fake_apply

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.terraform", mock_terraform),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    return mock_repo


def test_provision_task_on_progress_called_and_cleared():
    """on_progress is wired: adapter message written, then cleared on READY."""
    booking_id = str(uuid4())
    mock_repo = _run_provision(booking_id, str(uuid4()), str(uuid4()))

    msg_calls = [c.args[2] for c in mock_repo.sync_set_status_message.call_args_list]
    assert "Provisioning (stub mode)…" in msg_calls
    assert None in msg_calls  # cleared on success
    assert msg_calls[-1] is None  # last message is the clear


def test_provision_task_sets_failure_message():
    """on_progress is set to failure message when apply raises."""
    booking_id = str(uuid4())
    mock_repo = _run_provision(
        booking_id, str(uuid4()), str(uuid4()),
        terraform_apply_side_effect=RuntimeError("VCD error"),
    )

    msg_calls = [c.args[2] for c in mock_repo.sync_set_status_message.call_args_list]
    assert "Failed — see audit log" in msg_calls


# ---------------------------------------------------------------------------
# teardown_vm_task
# ---------------------------------------------------------------------------

def _run_teardown(booking_id, *, terraform_destroy_side_effect=None):
    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()
    mock_booking.vm_password = "pass"
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=MagicMock(vapp_template_id="tpl"))
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=26624))

    async def fake_destroy(workspace_id, config, api_token=None, on_progress=None, force=False):
        if terraform_destroy_side_effect:
            raise terraform_destroy_side_effect
        if on_progress:
            on_progress("Destroying (stub mode)…")

    mock_terraform = MagicMock()
    mock_terraform.destroy = fake_destroy

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_sf,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.terraform", mock_terraform),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    return mock_repo


def test_teardown_task_on_progress_called_and_cleared():
    """Teardown adapter message written, then cleared on RELEASED."""
    mock_repo = _run_teardown(str(uuid4()))

    msg_calls = [c.args[2] for c in mock_repo.sync_set_status_message.call_args_list]
    assert "Destroying (stub mode)…" in msg_calls
    assert None in msg_calls
    assert msg_calls[-1] is None


def test_teardown_task_sets_failure_message():
    mock_repo = _run_teardown(
        str(uuid4()),
        terraform_destroy_side_effect=RuntimeError("destroy failed"),
    )

    msg_calls = [c.args[2] for c in mock_repo.sync_set_status_message.call_args_list]
    assert "Teardown failed — see audit log" in msg_calls
