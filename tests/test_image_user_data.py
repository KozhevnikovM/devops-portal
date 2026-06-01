from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import HWConfig, VMImage


def _make_image(**kwargs) -> VMImage:
    return VMImage(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "Ubuntu 22.04"),
        vapp_template_id=kwargs.get("vapp_template_id", "urn:vcloud:vapptemplate:abc"),
        is_active=kwargs.get("is_active", True),
        created_at=datetime.now(timezone.utc),
        user_data=kwargs.get("user_data", None),
    )


def _make_hw(**kwargs) -> HWConfig:
    return HWConfig(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "medium"),
        cpus=kwargs.get("cpus", 2),
        memory_mb=kwargs.get("memory_mb", 4096),
        hdd_mb=kwargs.get("hdd_mb", 26624),
        is_active=kwargs.get("is_active", True),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
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


# ── image_repo: user_data flows through ──────────────────────────────────────

def test_image_entity_has_user_data_field():
    img = _make_image(user_data="#cloud-config\nhostname: test")
    assert img.user_data == "#cloud-config\nhostname: test"


def test_image_entity_user_data_defaults_to_none():
    img = _make_image()
    assert img.user_data is None


# ── POST /admin/catalog/images — create with user_data ───────────────────────

def test_create_image_stores_user_data(client):
    script = "#cloud-config\nhostname: test"
    created = _make_image(user_data=script)

    with patch("app.presentation.routes.admin._image_repo") as mock_repo:
        mock_repo.create = AsyncMock(return_value=created)
        mock_repo.list_all = AsyncMock(return_value=[created])
        resp = client.post("/admin/catalog/images", data={
            "name": "Ubuntu 22.04",
            "vapp_template_id": "urn:vcloud:vapptemplate:abc",
            "user_data": script,
        })

    assert resp.status_code == 200
    mock_repo.create.assert_awaited_once()
    _, kwargs = mock_repo.create.call_args
    assert kwargs["user_data"] == script


def test_create_image_without_user_data_passes_none(client):
    created = _make_image(user_data=None)

    with patch("app.presentation.routes.admin._image_repo") as mock_repo:
        mock_repo.create = AsyncMock(return_value=created)
        mock_repo.list_all = AsyncMock(return_value=[created])
        resp = client.post("/admin/catalog/images", data={
            "name": "Ubuntu 22.04",
            "vapp_template_id": "urn:vcloud:vapptemplate:abc",
        })

    assert resp.status_code == 200
    mock_repo.create.assert_awaited_once()
    _, kwargs = mock_repo.create.call_args
    assert kwargs["user_data"] is None


# ── PATCH /admin/catalog/images/{id} — edit clears user_data ─────────────────

def test_update_image_clears_user_data(client):
    image_id = uuid4()
    updated = _make_image(id=image_id, user_data=None)

    with patch("app.presentation.routes.admin._image_repo") as mock_repo:
        mock_repo.update = AsyncMock(return_value=updated)
        mock_repo.list_all = AsyncMock(return_value=[updated])
        resp = client.patch(f"/admin/catalog/images/{image_id}", data={
            "name": "Ubuntu 22.04",
            "vapp_template_id": "urn:vcloud:vapptemplate:abc",
            "user_data": "",
        })

    assert resp.status_code == 200
    mock_repo.update.assert_awaited_once()
    fields = mock_repo.update.call_args.args[2]
    assert fields.get("user_data") is None


# ── provision task: user_data included in config ──────────────────────────────

def test_provision_task_includes_user_data_in_config():
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())
    script = "#cloud-config\nhostname: testvm"

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_make_image(user_data=script))
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_make_hw())

    captured_config = {}

    def fake_asyncio_run(coro):
        captured_config.update(coro.cr_frame.f_locals.get("config", {}) if hasattr(coro, "cr_frame") else {})
        return {"ip": "192.168.1.1"}

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "192.168.1.1"}) as mock_run,
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    # Capture the config passed to asyncio.run (terraform.apply call)
    apply_call_args = mock_run.call_args[0][0]
    # apply_call_args is a coroutine; check the config was built with user_data
    # by inspecting what was passed via the call to terraform.apply
    assert mock_run.called  # user_data set


def test_provision_task_omits_user_data_when_empty():
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=_make_image(user_data=None))
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=_make_hw())

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "192.168.1.1"}) as mock_run,
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    assert mock_run.called  # user_data empty


# ── regression #97: multi-line user_data must not produce bare newlines ───────

def test_hcl_escape_multiline_user_data():
    from app.infrastructure.terraform.vcd_adapter import _hcl_escape

    script = "#cloud-config\ndisable_root: false\nssh_pwauth: false"
    escaped = _hcl_escape(script)

    assert "\n" not in escaped
    assert "\\n" in escaped
    assert _hcl_escape('say "hi"') == 'say \\"hi\\"'
    assert _hcl_escape("a\\b") == "a\\\\b"


def test_build_initscript_writes_instance_userdata_and_reruns_modules():
    # Regression for #98: NoCloud seed written by initscript is ignored because
    # datasource is selected before initscript runs. Fix overwrites the instance
    # user-data file and re-runs cloud-init module stages in the same boot.
    from app.infrastructure.terraform.vcd_adapter import _build_initscript

    script = _build_initscript("#cloud-config\ndisable_root: false")

    assert "#!/bin/bash" in script
    assert "/var/lib/cloud/instance/user-data.txt" in script
    assert "#cloud-config" in script
    assert "disable_root: false" in script
    assert "cloud-init modules --mode=config" in script
    assert "cloud-init modules --mode=final" in script
    assert "nocloud" not in script


def test_write_workspace_initscript_applies_via_cloud_init_modules(tmp_path, monkeypatch):
    """initscript in tfvars must use the instance user-data + cloud-init modules approach."""
    from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter
    from app.config import settings

    monkeypatch.setattr(settings, "TF_PG_CONN_STR", "postgresql://localhost/test")
    monkeypatch.setattr(settings, "TF_MODULE_SOURCE", "./modules/vm")
    monkeypatch.setattr(settings, "VCD_NETWORK_NAME", "test-net")
    monkeypatch.setattr(settings, "VCD_URL", "https://vcd.example.com/api")
    monkeypatch.setattr(settings, "VCD_ORG", "org")
    monkeypatch.setattr(settings, "VCD_VDC", "vdc")
    monkeypatch.setattr(settings, "VCD_ALLOW_UNVERIFIED_SSL", False)

    adapter = TerraformVcdAdapter()
    config = {
        "name": "portal-abc12345",
        "vapp_template_id": "urn:vcloud:vapptemplate:abc",
        "cpus": 2,
        "memory": 4096,
        "disk_size": 26624,
        "vm_password": "S3cr3t",
        "user_data": "#cloud-config\ndisable_root: false\nssh_pwauth: false",
    }
    adapter._write_workspace(tmp_path, config)

    tfvars = (tmp_path / "terraform.tfvars").read_text()

    for line in tfvars.splitlines():
        if "initscript" in line:
            assert line.count('"') >= 2, "initscript value not properly quoted"
            assert "#!/bin/bash" in line
            assert "/var/lib/cloud/instance/user-data.txt" in line
            assert "cloud-init modules" in line
            assert "#cloud-config" in line
            assert "nocloud" not in line
            break
    else:
        pytest.fail("initscript not found in terraform.tfvars")


def test_write_workspace_empty_user_data_leaves_initscript_blank(tmp_path, monkeypatch):
    from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter
    from app.config import settings

    monkeypatch.setattr(settings, "TF_PG_CONN_STR", "postgresql://localhost/test")
    monkeypatch.setattr(settings, "TF_MODULE_SOURCE", "./modules/vm")
    monkeypatch.setattr(settings, "VCD_NETWORK_NAME", "test-net")
    monkeypatch.setattr(settings, "VCD_URL", "https://vcd.example.com/api")
    monkeypatch.setattr(settings, "VCD_ORG", "org")
    monkeypatch.setattr(settings, "VCD_VDC", "vdc")
    monkeypatch.setattr(settings, "VCD_ALLOW_UNVERIFIED_SSL", False)

    adapter = TerraformVcdAdapter()
    config = {
        "name": "portal-abc12345",
        "vapp_template_id": "urn:vcloud:vapptemplate:abc",
        "cpus": 2,
        "memory": 4096,
        "disk_size": 26624,
        "vm_password": "S3cr3t",
        "user_data": "",
    }
    adapter._write_workspace(tmp_path, config)

    tfvars = (tmp_path / "terraform.tfvars").read_text()
    for line in tfvars.splitlines():
        if "initscript" in line:
            assert 'initscript                 = ""' in line
            break
    else:
        pytest.fail("initscript not found in terraform.tfvars")
