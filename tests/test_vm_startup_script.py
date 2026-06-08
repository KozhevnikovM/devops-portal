"""Tests for the VM startup bash script over SSH (v0.8.0 P1.2, #205).

A VM booking can carry a `startup_script` that the worker runs over SSH in the CONFIGURING state.
The SSH transport (paramiko) is exercised via injected fakes so the tests need no real VM.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.infrastructure.config.runner import (
    ConfigScriptError, SshConfigRunner, StubConfigRunner, VmUnreachableError,
)


# ── ConfigRunner logic ────────────────────────────────────────────────────────
def _fake_client(stdout_lines, exit_code, stderr=b""):
    """A fake paramiko SSHClient whose exec_command yields the given output + exit code."""
    chan = MagicMock()
    chan.recv_exit_status.return_value = exit_code
    stdout = MagicMock()
    stdout.__iter__ = lambda self: iter(stdout_lines)
    stdout.channel = chan
    stderr_obj = MagicMock()
    stderr_obj.read.return_value = stderr
    stdin = MagicMock()
    client = MagicMock()
    client.exec_command.return_value = (stdin, stdout, stderr_obj)
    return client


def test_run_script_streams_and_succeeds():
    client = _fake_client(["installing\n", "done\n"], exit_code=0)
    progress = []
    SshConfigRunner.run_script(client, "echo hi", on_progress=progress.append)
    # bash -s was fed the script over stdin.
    client.exec_command.assert_called_once_with("bash -s")
    stdin = client.exec_command.return_value[0]
    stdin.write.assert_called_once_with("echo hi")
    assert progress  # output streamed


def test_run_script_nonzero_exit_raises():
    client = _fake_client(["boom\n"], exit_code=2, stderr=b"permission denied")
    with pytest.raises(ConfigScriptError) as exc:
        SshConfigRunner.run_script(client, "false", on_progress=None)
    assert "exit 2" in str(exc.value)


def test_stub_runner_is_noop():
    runner = StubConfigRunner()
    client = runner.connect("10.0.0.5", "pw")
    runner.run_script(client, "echo hi")
    runner.close(client)


def test_connect_retries_then_succeeds():
    """connect retries on failure and returns the client once SSH answers."""
    import sys
    fake_paramiko = MagicMock()
    good = MagicMock()
    # First two SSHClient()s fail to connect, the third succeeds.
    bad1, bad2 = MagicMock(), MagicMock()
    bad1.connect.side_effect = OSError("refused")
    bad2.connect.side_effect = OSError("refused")
    good.connect.return_value = None
    fake_paramiko.SSHClient.side_effect = [bad1, bad2, good]
    runner = SshConfigRunner()
    with patch.dict(sys.modules, {"paramiko": fake_paramiko}), \
         patch("app.infrastructure.config.runner.settings") as s, \
         patch("app.infrastructure.config.runner.time.sleep") as sleep, \
         patch("app.infrastructure.config.runner.time.monotonic", side_effect=[0, 0, 30, 60, 90, 120]):
        s.VM_SSH_PRIVATE_KEY = ""
        s.VM_SSH_PORT = 22
        s.VM_SSH_USER = "root"
        s.CONFIG_SSH_TIMEOUT = 300
        s.CONFIG_SSH_RETRY_INTERVAL = 30
        client = runner.connect("10.0.0.5", "pw", on_progress=None)
    assert client is good
    assert sleep.call_count == 2  # waited between the two failures


def test_connect_times_out_to_vm_unreachable():
    """connect raises VmUnreachableError when SSH never comes up within the timeout."""
    import sys
    fake_paramiko = MagicMock()
    fake_paramiko.SSHClient.return_value.connect.side_effect = OSError("connection refused")
    runner = SshConfigRunner()
    with patch.dict(sys.modules, {"paramiko": fake_paramiko}), \
         patch("app.infrastructure.config.runner.settings") as s, \
         patch("app.infrastructure.config.runner.time.sleep"):
        s.VM_SSH_PRIVATE_KEY = ""
        s.VM_SSH_PORT = 22
        s.VM_SSH_USER = "root"
        s.CONFIG_SSH_TIMEOUT = 0  # deadline already passed → immediate failure
        s.CONFIG_SSH_RETRY_INTERVAL = 30
        with pytest.raises(VmUnreachableError):
            runner.connect("10.0.0.5", "pw", on_progress=None)


# ── _needs_configuration + selection ──────────────────────────────────────────
def test_needs_configuration_tracks_startup_script():
    from app.tasks.provision import _needs_configuration
    assert _needs_configuration(SimpleNamespace(startup_script="echo hi")) is True
    assert _needs_configuration(SimpleNamespace(startup_script=None)) is False


def test_stub_mode_selects_stub_runner():
    from app.infrastructure.config.runner import build_config_runner
    with patch("app.infrastructure.config.runner.settings.USE_STUB_TERRAFORM", True):
        assert isinstance(build_config_runner(), StubConfigRunner)
    with patch("app.infrastructure.config.runner.settings.USE_STUB_TERRAFORM", False):
        assert isinstance(build_config_runner(), SshConfigRunner)


# ── Provision orchestration: reachability vs config outcomes ──────────────────
def _provision_in_real_mode(startup_script, *, connect_exc=None, script_exc=None):
    """Run provision_vm_task in real mode with a stubbed config runner; return the status calls."""
    bid, iid, hid = str(uuid4()), str(uuid4()), str(uuid4())
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=SimpleNamespace(startup_script=startup_script, config_roles=[]))
    img = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(
        id=iid, name="Ubuntu", vapp_template_id="t")))
    hw = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(
        id=hid, name="medium", cpus=2, memory_mb=4096, disk_mb=26624, drive_type="HDD")))
    runner = MagicMock()
    if connect_exc:
        runner.connect.side_effect = connect_exc
    else:
        runner.connect.return_value = MagicMock()
    if script_exc:
        runner.run_script.side_effect = script_exc

    with (
        patch("app.tasks.provision.SyncSessionLocal") as sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", img),
        patch("app.tasks.provision.hw_config_repo", hw),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.5"}),
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.config_runner", runner),
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[bid, iid, hid])
    return mock_repo.sync_update_status.call_args_list, runner


def test_unreachable_vm_marks_failed_not_ready():
    from app.domain.enums import BookingStatus
    calls, _ = _provision_in_real_mode("echo hi", connect_exc=VmUnreachableError("no ssh"))
    statuses = [c.args[2] for c in calls]
    assert BookingStatus.READY not in statuses
    assert statuses[-1] == BookingStatus.FAILED  # retries exhausted eagerly under .apply()


def test_reachable_script_failure_is_ready_but_config_failed():
    from app.domain.enums import BookingStatus
    calls, runner = _provision_in_real_mode("bad", script_exc=ConfigScriptError("exit 1"))
    runner.run_script.assert_called_once()
    ready = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready.kwargs.get("config_failed") is True


def test_reachable_script_success_is_clean_ready():
    from app.domain.enums import BookingStatus
    calls, runner = _provision_in_real_mode("echo hi")
    runner.run_script.assert_called_once()
    ready = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready.kwargs.get("config_failed") is False


def test_script_less_vm_still_waits_for_reachability():
    from app.domain.enums import BookingStatus
    calls, runner = _provision_in_real_mode(None)
    runner.connect.assert_called_once()      # reachability checked
    runner.run_script.assert_not_called()    # nothing to run
    ready = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready.kwargs.get("config_failed") is False


# ── Persistence + order API ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_use_case_persists_startup_script():
    from app.application.use_cases.create_booking import CreateBookingUseCase

    created = {}

    async def fake_create(session, booking):
        created["startup_script"] = booking.startup_script
        return booking

    repo = MagicMock()
    repo.create = AsyncMock(side_effect=fake_create)
    image_repo = MagicMock()
    image_repo.get = AsyncMock(return_value=SimpleNamespace(
        id=uuid4(), name="Ubuntu", vapp_template_id="t"))
    hw_repo = MagicMock()
    hw_repo.get = AsyncMock(return_value=SimpleNamespace(
        id=uuid4(), name="medium", cpus=2, memory_mb=4096, disk_mb=26624, drive_type="HDD"))
    quota_repo = MagicMock()
    quota_repo.get_limits_for_update = AsyncMock(return_value={
        "max_cpus": 100, "max_memory_gb": 100, "max_ssd_gb": 100, "max_hdd_gb": 100})
    quota_repo.count_active_resources = AsyncMock(return_value={
        "cpus": 0, "memory_gb": 0, "ssd_gb": 0, "hdd_gb": 0})
    dispatcher = MagicMock()

    uc = CreateBookingUseCase(repo, image_repo, hw_repo, quota_repo=quota_repo, dispatcher=dispatcher)
    await uc.execute(MagicMock(), 240, uuid4(), uuid4(), user_id="u1",
                     startup_script="echo hi")

    assert created["startup_script"] == "echo hi"


def test_order_api_threads_startup_script():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user
    from app.domain.entities import Booking
    from app.domain.enums import BookingStatus, ResourceType
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    try:
        with patch("app.presentation.routes.api_bookings._image_repo") as img, \
             patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
             patch("app.presentation.routes.api_bookings._create_use_case") as uc:
            img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
            hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
            uc.execute = AsyncMock(return_value=booking)
            resp = TestClient(app).post("/api/bookings", json={
                "resource_type": "VM", "ttl_minutes": 240,
                "image_name": "Ubuntu", "hw_config_name": "medium",
                "startup_script": "echo hi",
            })
        assert resp.status_code == 201
        assert uc.execute.call_args.kwargs["startup_script"] == "echo hi"
    finally:
        app.dependency_overrides.clear()
