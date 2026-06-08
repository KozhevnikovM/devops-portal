"""Tests for the Environment blueprint catalog (v0.8.0 P3.1, #208).

A blueprint is an admin-managed template bundling resource items (by name). This item is the
catalog only — ordering a blueprint is #209.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.entities import EnvironmentBlueprint, EnvironmentBlueprintItem


def _item(rt="VM", spec=None, label="web", pos=0):
    return EnvironmentBlueprintItem(
        id=uuid4(), resource_type=rt, position=pos, label=label,
        spec=spec if spec is not None else {"image_name": "Ubuntu", "hw_config_name": "medium"},
    )


def _bp(name="dev-stack", items=None, is_active=True):
    return EnvironmentBlueprint(
        id=uuid4(), name=name, description="ns + web", is_active=is_active,
        created_at=datetime.now(timezone.utc), items=items if items is not None else [_item()],
    )


@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_admin, require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── items parsing ─────────────────────────────────────────────────────────────
def test_parse_blueprint_items():
    from app.presentation.routes.admin import _parse_blueprint_items
    items = _parse_blueprint_items('[{"resource_type":"NAMESPACE","spec":{}},'
                                   '{"resource_type":"VM","label":"web","spec":{"image_name":"U","hw_config_name":"m"}}]')
    assert [i["resource_type"] for i in items] == ["NAMESPACE", "VM"]
    assert items[1]["position"] == 1 and items[1]["label"] == "web"
    assert _parse_blueprint_items("") == []
    with pytest.raises(ValueError):
        _parse_blueprint_items('{"not":"an array"}')
    with pytest.raises(ValueError):
        _parse_blueprint_items('[{"resource_type":"BOGUS","spec":{}}]')
    with pytest.raises(ValueError):
        _parse_blueprint_items('[{"resource_type":"VM","spec":{}}]')  # VM needs image+hw


# ── /api/environment-blueprints ────────────────────────────────────────────────
def test_list_blueprints_readable_by_user(user_client):
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.list_all = AsyncMock(return_value=[_bp()])
        resp = user_client.get("/api/environment-blueprints")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["name"] == "dev-stack"
    assert body[0]["items"][0]["resource_type"] == "VM"


def test_create_blueprint_requires_admin(user_client):
    resp = user_client.post("/api/environment-blueprints", json={"name": "x", "items": []})
    assert resp.status_code == 403


def test_admin_creates_blueprint(admin_client):
    bp = _bp()
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.create = AsyncMock(return_value=bp)
        resp = admin_client.post("/api/environment-blueprints", json={
            "name": "dev-stack", "description": "ns + web",
            "items": [
                {"resource_type": "NAMESPACE", "spec": {}},
                {"resource_type": "VM", "label": "web",
                 "spec": {"image_name": "Ubuntu", "hw_config_name": "medium", "roles": ["docker-machine"]}},
            ],
        })
    assert resp.status_code == 201
    assert resp.json()["name"] == "dev-stack"
    # The repo got validated, position-stamped item dicts.
    items_arg = repo.create.call_args.args[3]
    assert items_arg[0]["position"] == 0 and items_arg[1]["position"] == 1


def test_create_blueprint_bad_resource_type_400(admin_client):
    resp = admin_client.post("/api/environment-blueprints", json={
        "name": "x", "items": [{"resource_type": "BOGUS", "spec": {}}],
    })
    assert resp.status_code == 400


def test_create_blueprint_vm_item_missing_image_400(admin_client):
    resp = admin_client.post("/api/environment-blueprints", json={
        "name": "x", "items": [{"resource_type": "VM", "spec": {"hw_config_name": "m"}}],
    })
    assert resp.status_code == 400


def test_create_blueprint_duplicate_409(admin_client):
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = admin_client.post("/api/environment-blueprints", json={"name": "dup", "items": []})
    assert resp.status_code == 409


# ── Admin UI ───────────────────────────────────────────────────────────────────
def test_admin_create_blueprint_via_ui(admin_client):
    with patch("app.presentation.routes.admin._blueprint_repo") as repo:
        repo.create = AsyncMock(return_value=_bp())
        repo.list_all = AsyncMock(return_value=[_bp()])
        resp = admin_client.post("/admin/catalog/blueprints", data={
            "name": "dev-stack", "description": "ns + web",
            "items": '[{"resource_type":"NAMESPACE","spec":{}}]',
        })
    assert resp.status_code == 200
    assert "blueprint-table" in resp.text
    assert "dev-stack" in resp.text


def test_admin_create_blueprint_invalid_items_shows_error(admin_client):
    with patch("app.presentation.routes.admin._blueprint_repo") as repo:
        repo.create = AsyncMock()
        resp = admin_client.post("/admin/catalog/blueprints", data={
            "name": "x", "items": "not json",
        })
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#blueprint-create-error"
    assert "valid JSON" in resp.text
    repo.create.assert_not_called()


# ── OpenAPI ───────────────────────────────────────────────────────────────────
def test_blueprints_endpoint_in_schema():
    from app.main import app
    paths = set(TestClient(app).get("/openapi.json").json()["paths"])
    assert "/api/environment-blueprints" in paths
