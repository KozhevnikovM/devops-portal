import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.domain.enums import BookingStatus
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter


@pytest.mark.asyncio
async def test_stub_adapter_returns_ip():
    adapter = StubTerraformAdapter()
    with patch("asyncio.sleep", return_value=None):
        result = await adapter.apply("workspace-test", {"cpu": 2})
    assert "ip" in result
    assert result["ip"].startswith("192.168.100.")


@pytest.mark.asyncio
async def test_stub_adapter_destroy_completes():
    adapter = StubTerraformAdapter()
    with patch("asyncio.sleep", return_value=None):
        await adapter.destroy("workspace-test")


def test_provision_task_sets_ready_status():
    booking_id = str(uuid4())
    fake_ip = "192.168.100.42"

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_repo.sync_update_status = MagicMock()

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": fake_ip}),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id])

    calls = mock_repo.sync_update_status.call_args_list
    statuses = [c.args[2] for c in calls]
    assert BookingStatus.PROVISIONING in statuses
    assert BookingStatus.READY in statuses

    ready_call = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready_call.kwargs.get("vm_ip") == fake_ip
