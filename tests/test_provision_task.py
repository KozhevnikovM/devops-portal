import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from app.domain.entities import VMTemplate
from app.domain.enums import BookingStatus
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter


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
    """Stub mode: semaphore is skipped, task reaches READY."""
    booking_id = str(uuid4())
    template_id = str(uuid4())
    fake_ip = "192.168.100.42"
    fake_template = _make_template(id=template_id)

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_template_repo = MagicMock()
    mock_template_repo.sync_get = MagicMock(return_value=fake_template)

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.template_repo", mock_template_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": fake_ip}),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, template_id])

    calls = mock_repo.sync_update_status.call_args_list
    statuses = [c.args[2] for c in calls]
    assert BookingStatus.PROVISIONING in statuses
    assert BookingStatus.READY in statuses

    ready_call = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready_call.kwargs.get("vm_ip") == fake_ip


def test_semaphore_acquired_and_released_on_success():
    """Real-adapter mode: lock is acquired before apply and released after."""
    booking_id = str(uuid4())
    template_id = str(uuid4())
    fake_ip = "10.0.0.1"
    fake_template = _make_template(id=template_id)

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_template_repo = MagicMock()
    mock_template_repo.sync_get = MagicMock(return_value=fake_template)

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "tok-a,tok-b"),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.template_repo", mock_template_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": fake_ip}),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, template_id])

    mock_redis.set.assert_called_once_with(
        "vcd_token_lock:0", "1", nx=True, ex=mock_redis.set.call_args.kwargs["ex"]
    )
    mock_redis.delete.assert_called_once_with("vcd_token_lock:0")

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.READY in statuses


def test_semaphore_released_on_failure():
    """Lock is released even when terraform apply raises."""
    booking_id = str(uuid4())
    template_id = str(uuid4())
    fake_template = _make_template(id=template_id)

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_template_repo = MagicMock()
    mock_template_repo.sync_get = MagicMock(return_value=fake_template)

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "tok-a"),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.template_repo", mock_template_repo),
        patch("app.tasks.provision.asyncio.run", side_effect=RuntimeError("VCD error")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, template_id])

    mock_redis.delete.assert_called_with("vcd_token_lock:0")


def test_provision_task_sets_retry_status_on_failure():
    """Intermediate failures set RETRY, not FAILED, while retries remain."""
    booking_id = str(uuid4())
    template_id = str(uuid4())
    fake_template = _make_template(id=template_id)

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_template_repo = MagicMock()
    mock_template_repo.sync_get = MagicMock(return_value=fake_template)

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.template_repo", mock_template_repo),
        patch("app.tasks.provision.asyncio.run", side_effect=RuntimeError("boom")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, template_id])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert statuses[-1] == BookingStatus.FAILED
    assert BookingStatus.RETRY in statuses
    assert statuses.count(BookingStatus.FAILED) == 1
