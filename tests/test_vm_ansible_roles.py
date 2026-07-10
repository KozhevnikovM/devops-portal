"""Tests for applying Ansible roles to a VM during configuration (v0.8.0 P2.2, #207).

Ordering a VM with `roles: [...]` snapshots the resolved roles onto the booking; the worker applies
them with ansible-playbook in the CONFIGURING step (subprocess mocked here).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.infrastructure.config.ansible import (
    AnsibleConfigError, AnsibleConfigRunner, _render_inventory, _render_playbook,
)

SNAPSHOT = [
    {"name": "docker-machine", "ansible_role": "docker_machine", "vars": {"docker_users": ["ubuntu"]}},
    {"name": "postgres-database", "ansible_role": "postgres_database", "vars": {}},
]


# ── Rendering ─────────────────────────────────────────────────────────────────
def test_render_inventory_password_auth():
    with patch("app.infrastructure.config.ansible.settings") as s:
        s.VM_SSH_USER = "root"; s.VM_SSH_PORT = 22; s.VM_SSH_PRIVATE_KEY = ""
        inv = _render_inventory("10.0.0.5", "pw")
    assert "ansible_host=10.0.0.5" in inv
    assert "ansible_password=pw" in inv
    assert "StrictHostKeyChecking=no" in inv


def test_render_playbook_lists_roles_with_vars():
    pb = _render_playbook(SNAPSHOT)
    assert "- role: docker_machine" in pb
    assert '"docker_users"' in pb
    assert "- role: postgres_database" in pb


# ── AnsibleConfigRunner ───────────────────────────────────────────────────────
def _booking(roles):
    return SimpleNamespace(id=uuid4(), config_roles=roles)


def test_apply_roles_runs_playbook():
    runner = AnsibleConfigRunner()
    proc = MagicMock()
    proc.stdout = iter(["PLAY [vm]\n", "ok=3\n"])
    proc.returncode = 0
    with patch("app.infrastructure.config.ansible.subprocess.Popen", return_value=proc) as popen, \
         patch("app.infrastructure.config.ansible.settings") as s:
        s.VM_SSH_USER = "root"; s.VM_SSH_PORT = 22; s.VM_SSH_PRIVATE_KEY = ""
        s.ANSIBLE_ROLES_PATH = "/app/ansible/roles"; s.ANSIBLE_TIMEOUT = 60
        s.ANSIBLE_VERBOSITY = 0
        runner.apply_roles(_booking(SNAPSHOT), ip="10.0.0.5", password="pw")
    # ansible-playbook was invoked.
    assert popen.call_args.args[0][0] == "ansible-playbook"


def test_apply_roles_nonzero_raises():
    runner = AnsibleConfigRunner()
    proc = MagicMock()
    proc.stdout = iter(["fatal: task failed\n"])
    proc.returncode = 2
    with patch("app.infrastructure.config.ansible.subprocess.Popen", return_value=proc), \
         patch("app.infrastructure.config.ansible.settings") as s:
        s.VM_SSH_USER = "root"; s.VM_SSH_PORT = 22; s.VM_SSH_PRIVATE_KEY = ""
        s.ANSIBLE_ROLES_PATH = "/app/ansible/roles"; s.ANSIBLE_TIMEOUT = 60
        s.ANSIBLE_VERBOSITY = 0
        with pytest.raises(AnsibleConfigError):
            runner.apply_roles(_booking(SNAPSHOT), ip="10.0.0.5", password="pw")


def test_apply_roles_noop_when_empty():
    runner = AnsibleConfigRunner()
    with patch("app.infrastructure.config.ansible.subprocess.Popen") as popen:
        runner.apply_roles(_booking([]), ip="10.0.0.5", password="pw")
    popen.assert_not_called()


# ── Order API: roles → snapshot ───────────────────────────────────────────────
@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _vm_booking():
    from datetime import datetime, timedelta, timezone
    from app.domain.entities import Booking
    from app.domain.enums import BookingStatus, ResourceType
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


def test_order_vm_with_roles_snapshots(client):
    booking = _vm_booking()
    role = SimpleNamespace(name="docker-machine", ansible_role="docker_machine", default_vars={"v": 1}, secret_vars={})
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.api_bookings._role_repo") as roles, \
         patch("app.presentation.routes.api_bookings._create_use_case") as uc:
        img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        roles.get_by_name = AsyncMock(return_value=role)
        uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "Ubuntu", "hw_config_name": "medium",
            "roles": ["docker-machine"],
        })

    assert resp.status_code == 201
    snapshot = uc.execute.call_args.kwargs["config_roles"]
    assert snapshot == [{"name": "docker-machine", "ansible_role": "docker_machine", "vars": {"v": 1}, "secret_vars": {}}]


def test_order_vm_unknown_role_returns_400(client):
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.api_bookings._role_repo") as roles:
        img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        roles.get_by_name = AsyncMock(return_value=None)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "Ubuntu", "hw_config_name": "medium", "roles": ["nope"],
        })
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"]


# ── Provision: roles applied after the bash script ────────────────────────────
def test_roles_applied_after_script_clean_ready():
    from app.domain.enums import BookingStatus
    bid, iid, hid = str(uuid4()), str(uuid4()), str(uuid4())
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=SimpleNamespace(
        startup_script="echo hi", config_roles=SNAPSHOT, extra_vars={}, environment_label=None))
    img = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(id=iid, name="U", vapp_template_id="t")))
    hw = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(
        id=hid, name="m", cpus=2, memory_mb=4096, disk_mb=26624, drive_type="HDD")))
    cfg = MagicMock(); cfg.connect.return_value = MagicMock()
    ans = MagicMock()
    with (
        patch("app.tasks.provision.SyncSessionLocal") as sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", img),
        patch("app.tasks.provision.hw_config_repo", hw),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.5"}),
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.config_runner", cfg),
        patch("app.tasks.provision.ansible_runner", ans),
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[bid, iid, hid])

    cfg.run_script.assert_called_once()      # bash first
    ans.apply_roles.assert_called_once()     # then roles
    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    ready = next(c for c in mock_repo.sync_update_status.call_args_list if c.args[2] == BookingStatus.READY)
    assert ready.kwargs.get("config_failed") is False


def test_role_failure_is_ready_but_config_failed():
    from app.domain.enums import BookingStatus
    bid, iid, hid = str(uuid4()), str(uuid4()), str(uuid4())
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=SimpleNamespace(
        startup_script=None, config_roles=SNAPSHOT, extra_vars={}, environment_label=None))
    img = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(id=iid, name="U", vapp_template_id="t")))
    hw = MagicMock(sync_get=MagicMock(return_value=SimpleNamespace(
        id=hid, name="m", cpus=2, memory_mb=4096, disk_mb=26624, drive_type="HDD")))
    cfg = MagicMock(); cfg.connect.return_value = MagicMock()
    ans = MagicMock(); ans.apply_roles.side_effect = AnsibleConfigError("role failed")
    with (
        patch("app.tasks.provision.SyncSessionLocal") as sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", img),
        patch("app.tasks.provision.hw_config_repo", hw),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.5"}),
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.config_runner", cfg),
        patch("app.tasks.provision.ansible_runner", ans),
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[bid, iid, hid])

    ready = next(c for c in mock_repo.sync_update_status.call_args_list if c.args[2] == BookingStatus.READY)
    assert ready.kwargs.get("config_failed") is True
