"""Tests for the VM startup bash script over SSH (v0.8.0 P1.2, #205).

A VM booking can carry a `startup_script` that the worker runs over SSH in the CONFIGURING state.
The SSH transport (paramiko) is exercised via injected fakes so the tests need no real VM.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.infrastructure.config.runner import ConfigError, SshConfigRunner, StubConfigRunner


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
    SshConfigRunner._run_script(client, "echo hi", on_progress=progress.append)
    # bash -s was fed the script over stdin.
    client.exec_command.assert_called_once_with("bash -s")
    stdin = client.exec_command.return_value[0]
    stdin.write.assert_called_once_with("echo hi")
    assert progress  # output streamed


def test_run_script_nonzero_exit_raises():
    client = _fake_client(["boom\n"], exit_code=2, stderr=b"permission denied")
    with pytest.raises(ConfigError) as exc:
        SshConfigRunner._run_script(client, "false", on_progress=None)
    assert "exit 2" in str(exc.value)


def test_ssh_runner_skips_when_no_script():
    runner = SshConfigRunner()
    booking = SimpleNamespace(id=uuid4(), startup_script=None)
    with patch.object(runner, "_connect") as connect:
        runner.run(booking, ip="10.0.0.5", password="pw")
    connect.assert_not_called()


def test_ssh_runner_connects_then_runs_then_closes():
    runner = SshConfigRunner()
    booking = SimpleNamespace(id=uuid4(), startup_script="echo hi")
    client = _fake_client(["ok\n"], exit_code=0)
    with patch.object(runner, "_connect", return_value=client) as connect:
        runner.run(booking, ip="10.0.0.5", password="pw", on_progress=None)
    connect.assert_called_once()
    client.close.assert_called_once()


def test_stub_runner_is_noop():
    StubConfigRunner().run(SimpleNamespace(id=uuid4(), startup_script="echo hi"), ip="x", password="y")


def test_connect_times_out_to_config_error():
    """_connect raises ConfigError when SSH never comes up within the timeout."""
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
        s.CONFIG_SSH_TIMEOUT = 0  # deadline already passed → immediate ConfigError
        with pytest.raises(ConfigError):
            runner._connect("10.0.0.5", "pw", on_progress=None)


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
