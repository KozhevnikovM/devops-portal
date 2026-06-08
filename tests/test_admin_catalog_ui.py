from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.entities import HWConfig, VMImage


def _make_image(**kwargs) -> VMImage:
    return VMImage(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "Ubuntu 22.04"),
        vapp_template_id=kwargs.get("vapp_template_id", "urn:vcloud:vapptemplate:abc"),
        is_active=kwargs.get("is_active", True),
        created_at=datetime.now(timezone.utc),
    )


def _make_hw(**kwargs) -> HWConfig:
    return HWConfig(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "medium"),
        cpus=kwargs.get("cpus", 2),
        memory_mb=kwargs.get("memory_mb", 4096),
        disk_mb=kwargs.get("disk_mb", 26624),
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


# ── GET /admin/catalog ────────────────────────────────────────────────────────

def test_catalog_page_returns_200(client):
    images = [_make_image()]
    hw_configs = [_make_hw()]
    with (
        patch("app.presentation.routes.admin._image_repo") as mock_img,
        patch("app.presentation.routes.admin._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.admin._namespace_repo") as mock_ns,
        patch("app.presentation.routes.admin._static_vm_repo") as mock_svm,
        patch("app.presentation.routes.admin._role_repo") as mock_role,
         patch("app.presentation.routes.admin._blueprint_repo") as mock_bp,
    ):
        mock_img.list_all = AsyncMock(return_value=images)
        mock_hw.list_all = AsyncMock(return_value=hw_configs)
        mock_ns.list_all = AsyncMock(return_value=[])
        mock_ns.held_by = AsyncMock(return_value={})
        mock_svm.list_all = AsyncMock(return_value=[])
        mock_svm.held_by = AsyncMock(return_value={})
        mock_role.list_all = AsyncMock(return_value=[])
        mock_bp.list_all = AsyncMock(return_value=[])
        resp = client.get("/admin/catalog")

    assert resp.status_code == 200
    assert "Ubuntu 22.04" in resp.text
    assert "medium" in resp.text


def test_catalog_page_requires_admin():
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    from tests.conftest import make_fake_admin

    non_admin = make_fake_admin()
    non_admin = non_admin.__class__(
        id=non_admin.id, username="user", password_hash="",
        role="user", is_active=True, created_at=non_admin.created_at,
    )
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_admin] = lambda: (_ for _ in ()).throw(
        __import__("fastapi").HTTPException(status_code=403)
    )
    try:
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/admin/catalog")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()


# ── POST /admin/catalog/images ────────────────────────────────────────────────

def test_create_image_returns_updated_table(client):
    new_image = _make_image(name="Debian 12")
    images = [new_image]
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.create = AsyncMock(return_value=new_image)
        mock_img.list_all = AsyncMock(return_value=images)
        resp = client.post(
            "/admin/catalog/images",
            data={"name": "Debian 12", "vapp_template_id": "urn:vcloud:vapptemplate:xyz"},
        )

    assert resp.status_code == 200
    assert "Debian 12" in resp.text
    assert "image-table" in resp.text


def test_create_image_duplicate_returns_error_fragment(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/catalog/images",
            data={"name": "Ubuntu 22.04", "vapp_template_id": "urn:vcloud:vapptemplate:abc"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#image-create-error"
    assert "already exists" in resp.text


# ── GET /admin/catalog/images/{id}/edit ──────────────────────────────────────

def test_edit_image_form_returns_table_with_edit_row(client):
    img = _make_image()
    images = [img]
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.list_all = AsyncMock(return_value=images)
        resp = client.get(f"/admin/catalog/images/{img.id}/edit")

    assert resp.status_code == 200
    assert 'hx-patch="/admin/catalog/images/' in resp.text


def test_edit_image_form_404_for_missing(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.list_all = AsyncMock(return_value=[])
        resp = client.get(f"/admin/catalog/images/{uuid4()}/edit")

    assert resp.status_code == 404


# ── PATCH /admin/catalog/images/{id} ─────────────────────────────────────────

def test_update_image_returns_updated_table(client):
    img = _make_image()
    updated = _make_image(id=img.id, name="Updated Name")
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.update = AsyncMock(return_value=updated)
        mock_img.list_all = AsyncMock(return_value=[updated])
        resp = client.patch(
            f"/admin/catalog/images/{img.id}",
            data={"name": "Updated Name", "vapp_template_id": img.vapp_template_id},
        )

    assert resp.status_code == 200
    assert "Updated Name" in resp.text


def test_update_image_404_for_missing(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.update = AsyncMock(side_effect=ValueError("not found"))
        resp = client.patch(
            f"/admin/catalog/images/{uuid4()}",
            data={"name": "X", "vapp_template_id": "urn:x"},
        )

    assert resp.status_code == 404


# ── DELETE /admin/catalog/images/{id} ────────────────────────────────────────

def test_deactivate_image_returns_updated_table(client):
    img = _make_image()
    deactivated = _make_image(id=img.id, is_active=False)
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.deactivate = AsyncMock()
        mock_img.list_all = AsyncMock(return_value=[deactivated])
        resp = client.delete(f"/admin/catalog/images/{img.id}")

    assert resp.status_code == 200
    assert "inactive" in resp.text


def test_deactivate_image_404_for_missing(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.deactivate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/images/{uuid4()}")

    assert resp.status_code == 404


# ── POST /admin/catalog/hardware ─────────────────────────────────────────────

def test_create_hw_config_returns_updated_table(client):
    hw = _make_hw(name="xlarge")
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.create = AsyncMock(return_value=hw)
        mock_hw.list_all = AsyncMock(return_value=[hw])
        resp = client.post(
            "/admin/catalog/hardware",
            data={"name": "xlarge", "cpus": "8", "memory_gb": "16", "disk_gb": "100"},
        )

    assert resp.status_code == 200
    assert "xlarge" in resp.text
    assert "hw-table" in resp.text


def test_create_hw_config_duplicate_returns_error_fragment(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/catalog/hardware",
            data={"name": "medium", "cpus": "2", "memory_gb": "4", "disk_gb": "26"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#hw-create-error"
    assert "already exists" in resp.text


# ── GET /admin/catalog/hardware/{id}/edit ────────────────────────────────────

def test_edit_hw_config_form_returns_table_with_edit_row(client):
    hw = _make_hw()
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.list_all = AsyncMock(return_value=[hw])
        resp = client.get(f"/admin/catalog/hardware/{hw.id}/edit")

    assert resp.status_code == 200
    assert 'hx-patch="/admin/catalog/hardware/' in resp.text


def test_edit_hw_config_form_404_for_missing(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.list_all = AsyncMock(return_value=[])
        resp = client.get(f"/admin/catalog/hardware/{uuid4()}/edit")

    assert resp.status_code == 404


# ── PATCH /admin/catalog/hardware/{id} ───────────────────────────────────────

def test_update_hw_config_returns_updated_table(client):
    hw = _make_hw()
    updated = _make_hw(id=hw.id, cpus=8)
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.update = AsyncMock(return_value=updated)
        mock_hw.list_all = AsyncMock(return_value=[updated])
        resp = client.patch(
            f"/admin/catalog/hardware/{hw.id}",
            data={"name": hw.name, "cpus": "8", "memory_gb": "4", "disk_gb": "26"},
        )

    assert resp.status_code == 200
    assert "8" in resp.text


def test_update_hw_config_404_for_missing(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.update = AsyncMock(side_effect=ValueError("not found"))
        resp = client.patch(
            f"/admin/catalog/hardware/{uuid4()}",
            data={"name": "x", "cpus": "1", "memory_gb": "1", "disk_gb": "1"},
        )

    assert resp.status_code == 404


# ── DELETE /admin/catalog/hardware/{id} ──────────────────────────────────────

def test_deactivate_hw_config_returns_updated_table(client):
    hw = _make_hw()
    deactivated = _make_hw(id=hw.id, is_active=False)
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.deactivate = AsyncMock()
        mock_hw.list_all = AsyncMock(return_value=[deactivated])
        resp = client.delete(f"/admin/catalog/hardware/{hw.id}")

    assert resp.status_code == 200
    assert "inactive" in resp.text


def test_deactivate_hw_config_404_for_missing(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.deactivate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/hardware/{uuid4()}")

    assert resp.status_code == 404


# ── POST /admin/catalog/images/{id}/activate ─────────────────────────────────

def test_activate_image_returns_updated_table(client):
    img = _make_image(is_active=False)
    activated = _make_image(id=img.id, is_active=True)
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.activate = AsyncMock()
        mock_img.list_all = AsyncMock(return_value=[activated])
        resp = client.post(f"/admin/catalog/images/{img.id}/activate")

    assert resp.status_code == 200
    assert "active" in resp.text


def test_activate_image_404_for_missing(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.activate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.post(f"/admin/catalog/images/{uuid4()}/activate")

    assert resp.status_code == 404


# ── DELETE /admin/catalog/images/{id}/permanent ──────────────────────────────

def test_hard_delete_image_returns_updated_table(client):
    img = _make_image(is_active=False)
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.delete = AsyncMock()
        mock_img.list_all = AsyncMock(return_value=[])
        resp = client.delete(f"/admin/catalog/images/{img.id}/permanent")

    assert resp.status_code == 200
    assert str(img.name) not in resp.text


def test_hard_delete_image_404_for_missing(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.delete = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/images/{uuid4()}/permanent")

    assert resp.status_code == 404


def test_hard_delete_image_409_when_bookings_exist(client):
    img = _make_image(is_active=False)
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.delete = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        resp = client.delete(f"/admin/catalog/images/{img.id}/permanent")

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == f"#image-delete-error-{img.id}"
    assert "Cannot delete" in resp.text


# ── POST /admin/catalog/hardware/{id}/activate ───────────────────────────────

def test_activate_hw_config_returns_updated_table(client):
    hw = _make_hw(is_active=False)
    activated = _make_hw(id=hw.id, is_active=True)
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.activate = AsyncMock()
        mock_hw.list_all = AsyncMock(return_value=[activated])
        resp = client.post(f"/admin/catalog/hardware/{hw.id}/activate")

    assert resp.status_code == 200
    assert "active" in resp.text


def test_activate_hw_config_404_for_missing(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.activate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.post(f"/admin/catalog/hardware/{uuid4()}/activate")

    assert resp.status_code == 404


# ── DELETE /admin/catalog/hardware/{id}/permanent ────────────────────────────

def test_hard_delete_hw_config_returns_updated_table(client):
    hw = _make_hw(is_active=False)
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.delete = AsyncMock()
        mock_hw.list_all = AsyncMock(return_value=[])
        resp = client.delete(f"/admin/catalog/hardware/{hw.id}/permanent")

    assert resp.status_code == 200


def test_hard_delete_hw_config_404_for_missing(client):
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.delete = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/hardware/{uuid4()}/permanent")

    assert resp.status_code == 404


def test_hard_delete_hw_config_409_when_bookings_exist(client):
    hw = _make_hw(is_active=False)
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_hw:
        mock_hw.delete = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        resp = client.delete(f"/admin/catalog/hardware/{hw.id}/permanent")

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == f"#hw-delete-error-{hw.id}"
    assert "Cannot delete" in resp.text
