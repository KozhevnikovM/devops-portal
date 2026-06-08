from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.entities import StaticVM


def _make_svm(**kwargs) -> StaticVM:
    return StaticVM(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "build-agent-1"),
        host=kwargs.get("host", "10.0.0.12"),
        username=kwargs.get("username", "ubuntu"),
        password=kwargs.get("password", "secret"),
        ssh_key=kwargs.get("ssh_key", None),
        cpus=kwargs.get("cpus", 2),
        memory_mb=kwargs.get("memory_mb", 4096),
        is_active=kwargs.get("is_active", True),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client():
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    from tests.conftest import make_fake_admin

    session_mock = AsyncMock()
    fake_admin = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_admin] = lambda: fake_admin
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Catalog page rendering ────────────────────────────────────────────────────

def test_catalog_page_renders_static_vm_availability(client):
    vm_free = _make_svm(name="free-vm", password="hunter2-plaintext")
    vm_booked = _make_svm(name="booked-vm")
    with (
        patch("app.presentation.routes.admin._image_repo") as mock_img,
        patch("app.presentation.routes.admin._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.admin._namespace_repo") as mock_ns,
        patch("app.presentation.routes.admin._static_vm_repo") as mock_svm,
        patch("app.presentation.routes.admin._role_repo") as mock_role,
    ):
        mock_img.list_all = AsyncMock(return_value=[])
        mock_hw.list_all = AsyncMock(return_value=[])
        mock_ns.list_all = AsyncMock(return_value=[])
        mock_ns.held_by = AsyncMock(return_value={})
        mock_svm.list_all = AsyncMock(return_value=[vm_free, vm_booked])
        mock_svm.held_by = AsyncMock(return_value={vm_booked.id: "alice"})
        mock_role.list_all = AsyncMock(return_value=[])
        resp = client.get("/admin/catalog")

    assert resp.status_code == 200
    assert "free-vm" in resp.text
    assert "Available" in resp.text
    assert "Booked by alice" in resp.text
    # password is masked, never rendered in plain text
    assert "hunter2-plaintext" not in resp.text


# ── POST /admin/catalog/static-vms ────────────────────────────────────────────

def test_create_static_vm_returns_updated_table(client):
    vm = _make_svm(name="new-vm")
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.create = AsyncMock(return_value=vm)
        mock_svm.list_all = AsyncMock(return_value=[vm])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": "new-vm", "host": "10.0.0.20", "username": "ubuntu",
                "password": "pw", "cpus": "2", "memory_gb": "4",
            },
        )

    assert resp.status_code == 200
    assert "new-vm" in resp.text
    assert "static-vm-table" in resp.text
    # args: session, name, host, username, password, ssh_key, cpus, memory_mb
    assert mock_svm.create.call_args.args[4] == "pw"  # password
    assert mock_svm.create.call_args.args[5] is None  # ssh_key
    # memory entered in GB is stored as MB
    assert mock_svm.create.call_args.args[7] == 4096


def test_create_static_vm_with_ssh_key_only(client):
    vm = _make_svm(name="key-vm", password=None, ssh_key="ssh-ed25519 AAAA")
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.create = AsyncMock(return_value=vm)
        mock_svm.list_all = AsyncMock(return_value=[vm])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": "key-vm", "host": "10.0.0.22", "username": "ubuntu",
                "password": "", "ssh_key": "ssh-ed25519 AAAA", "cpus": "", "memory_gb": "",
            },
        )

    assert resp.status_code == 200
    assert mock_svm.create.call_args.args[4] is None  # password
    assert mock_svm.create.call_args.args[5] == "ssh-ed25519 AAAA"  # ssh_key


def test_create_static_vm_requires_a_credential(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.create = AsyncMock()
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": "no-cred-vm", "host": "10.0.0.23", "username": "ubuntu",
                "password": "", "ssh_key": "", "cpus": "", "memory_gb": "",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#static-vm-create-error"
    assert "password or an SSH key" in resp.text
    mock_svm.create.assert_not_called()


def test_create_static_vm_optional_specs_blank(client):
    vm = _make_svm(name="bare-vm", cpus=None, memory_mb=None)
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.create = AsyncMock(return_value=vm)
        mock_svm.list_all = AsyncMock(return_value=[vm])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": "bare-vm", "host": "10.0.0.21", "username": "ubuntu",
                "password": "pw", "cpus": "", "memory_gb": "",
            },
        )

    assert resp.status_code == 200
    # blank cpus/memory → None (args: …, ssh_key[5], cpus[6], memory_mb[7])
    assert mock_svm.create.call_args.args[6] is None
    assert mock_svm.create.call_args.args[7] is None


def test_create_static_vm_duplicate_returns_error_fragment(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/catalog/static-vms",
            data={
                "name": "build-agent-1", "host": "10.0.0.12", "username": "ubuntu",
                "password": "pw", "cpus": "2", "memory_gb": "4",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#static-vm-create-error"
    assert "already exists" in resp.text


# ── GET edit / PATCH ──────────────────────────────────────────────────────────

def test_edit_static_vm_form_returns_table_with_edit_row(client):
    vm = _make_svm()
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.list_all = AsyncMock(return_value=[vm])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.get(f"/admin/catalog/static-vms/{vm.id}/edit")

    assert resp.status_code == 200
    assert 'hx-patch="/admin/catalog/static-vms/' in resp.text


def test_edit_static_vm_form_404_for_missing(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.list_all = AsyncMock(return_value=[])
        resp = client.get(f"/admin/catalog/static-vms/{uuid4()}/edit")

    assert resp.status_code == 404


def test_update_static_vm_returns_updated_table(client):
    vm = _make_svm()
    updated = _make_svm(id=vm.id, name="renamed-vm")
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.update = AsyncMock(return_value=updated)
        mock_svm.list_all = AsyncMock(return_value=[updated])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.patch(
            f"/admin/catalog/static-vms/{vm.id}",
            data={
                "name": "renamed-vm", "host": "10.0.0.12", "username": "ubuntu",
                "password": "pw", "cpus": "4", "memory_gb": "8",
            },
        )

    assert resp.status_code == 200
    assert "renamed-vm" in resp.text
    assert mock_svm.update.call_args.args[2]["memory_mb"] == 8192


def test_update_static_vm_404_for_missing(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.update = AsyncMock(side_effect=ValueError("not found"))
        resp = client.patch(
            f"/admin/catalog/static-vms/{uuid4()}",
            data={
                "name": "x", "host": "h", "username": "u",
                "password": "p", "ssh_key": "", "cpus": "", "memory_gb": "",
            },
        )

    assert resp.status_code == 404


# ── activate / deactivate / delete ────────────────────────────────────────────

def test_deactivate_static_vm_returns_updated_table(client):
    vm = _make_svm()
    deactivated = _make_svm(id=vm.id, is_active=False)
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.deactivate = AsyncMock()
        mock_svm.list_all = AsyncMock(return_value=[deactivated])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.delete(f"/admin/catalog/static-vms/{vm.id}")

    assert resp.status_code == 200
    assert "inactive" in resp.text


def test_activate_static_vm_returns_updated_table(client):
    vm = _make_svm(is_active=False)
    activated = _make_svm(id=vm.id, is_active=True)
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.activate = AsyncMock()
        mock_svm.list_all = AsyncMock(return_value=[activated])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.post(f"/admin/catalog/static-vms/{vm.id}/activate")

    assert resp.status_code == 200
    assert "active" in resp.text


def test_activate_static_vm_404_for_missing(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.activate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.post(f"/admin/catalog/static-vms/{uuid4()}/activate")

    assert resp.status_code == 404


def test_hard_delete_static_vm_returns_updated_table(client):
    vm = _make_svm(is_active=False)
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.delete = AsyncMock()
        mock_svm.list_all = AsyncMock(return_value=[])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.delete(f"/admin/catalog/static-vms/{vm.id}/permanent")

    assert resp.status_code == 200
    assert vm.name not in resp.text


def test_hard_delete_static_vm_404_for_missing(client):
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.delete = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/static-vms/{uuid4()}/permanent")

    assert resp.status_code == 404


def test_hard_delete_static_vm_blocked_when_bookings_exist(client):
    vm = _make_svm(is_active=False)
    with patch("app.presentation.routes.admin._static_vm_repo") as mock_svm:
        mock_svm.delete = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        resp = client.delete(f"/admin/catalog/static-vms/{vm.id}/permanent")

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == f"#static-vm-delete-error-{vm.id}"
    assert "Cannot delete" in resp.text


# ── Repository query semantics ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_available_excludes_inactive_and_held():
    from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

    repo = StaticVMRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.scalars = lambda: type("S", (), {"all": staticmethod(lambda: [])})()
    session.execute = AsyncMock(return_value=result)

    await repo.list_available(session)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "is_active" in compiled
    assert "static_vm_id" in compiled
    assert "bookings" in compiled
    # released/failed bookings do NOT hold a static VM
    assert "RELEASED" not in compiled
    assert "FAILED" not in compiled
    assert "READY" in compiled


@pytest.mark.asyncio
async def test_count_available_uses_same_filter():
    from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

    repo = StaticVMRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.scalar_one = lambda: 3
    session.execute = AsyncMock(return_value=result)

    count = await repo.count_available(session)
    assert count == 3
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "count" in compiled.lower()
    assert "is_active" in compiled


@pytest.mark.asyncio
async def test_held_by_excludes_terminal_bookings():
    from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

    repo = StaticVMRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.held_by(session)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "RELEASED" not in compiled
    assert "FAILED" not in compiled
    assert "READY" in compiled
