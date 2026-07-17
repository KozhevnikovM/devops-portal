"""Tests for the Ansible roles catalog (v0.8.0 P2.1, #206).

A Role is an admin-managed catalog entry (name, ansible_role, default_vars) with a JSON API
(`/api/roles`, list readable by any user) and an admin catalog UI panel.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.entities import Role


def _role(name="docker-machine", **kw):
    return Role(
        id=kw.get("id", uuid4()), name=name, description=kw.get("description", "Docker Engine"),
        ansible_role=kw.get("ansible_role", "docker_machine"),
        default_vars=kw.get("default_vars", {"version": "latest"}),
        is_active=kw.get("is_active", True), created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_admin, require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    # require_admin should reject a regular user, so DON'T override it here.
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


# ── default_vars parsing ──────────────────────────────────────────────────────
def test_parse_default_vars():
    from app.presentation.routes.admin import _parse_default_vars
    assert _parse_default_vars('{"a": 1}') == {"a": 1}
    assert _parse_default_vars("") == {}
    with pytest.raises(ValueError):
        _parse_default_vars("not json")
    with pytest.raises(ValueError):
        _parse_default_vars("[1, 2]")  # JSON array is not an object


# ── /api/roles ────────────────────────────────────────────────────────────────
def test_list_roles_readable_by_any_user(user_client):
    with patch("app.presentation.routes.api._role_repo") as repo:
        repo.list_all = AsyncMock(return_value=[_role()])
        resp = user_client.get("/api/roles")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["name"] == "docker-machine"
    assert body[0]["ansible_role"] == "docker_machine"
    assert body[0]["default_vars"] == {"version": "latest"}


def test_create_role_requires_admin(user_client):
    resp = user_client.post("/api/roles", json={
        "name": "x", "ansible_role": "x", "default_vars": {},
    })
    assert resp.status_code == 403


def test_admin_creates_role(admin_client):
    role = _role()
    with patch("app.presentation.routes.api._role_repo") as repo:
        repo.create = AsyncMock(return_value=role)
        resp = admin_client.post("/api/roles", json={
            "name": "docker-machine", "ansible_role": "docker_machine",
            "description": "Docker Engine", "default_vars": {"version": "latest"},
        })
    assert resp.status_code == 201
    assert resp.json()["name"] == "docker-machine"


def test_create_duplicate_role_returns_409(admin_client):
    with patch("app.presentation.routes.api._role_repo") as repo:
        repo.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = admin_client.post("/api/roles", json={"name": "dup", "ansible_role": "dup"})
    assert resp.status_code == 409


# ── Admin catalog UI ──────────────────────────────────────────────────────────
def test_admin_create_role_via_ui(admin_client):
    with patch("app.presentation.routes.admin._role_repo") as repo:
        repo.create = AsyncMock(return_value=_role())
        repo.list_all = AsyncMock(return_value=[_role()])
        resp = admin_client.post("/admin/catalog/roles", data={
            "name": "docker-machine", "ansible_role": "docker_machine",
            "description": "Docker", "default_vars": '{"version": "latest"}',
        })
    assert resp.status_code == 200
    assert "role-table" in resp.text
    assert "docker-machine" in resp.text


def test_admin_create_role_invalid_yaml_shows_error(admin_client):
    with patch("app.presentation.routes.admin._role_repo") as repo:
        repo.create = AsyncMock()
        # A YAML list is not a mapping — expect a validation error
        resp = admin_client.post("/admin/catalog/roles", data={
            "name": "x", "ansible_role": "x", "default_vars": "- item1\n- item2",
        })
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#role-create-error"
    assert "mapping" in resp.text
    repo.create.assert_not_called()


# ── OpenAPI ───────────────────────────────────────────────────────────────────
def test_roles_endpoint_in_schema():
    from app.main import app
    paths = set(TestClient(app).get("/openapi.json").json()["paths"])
    assert "/api/roles" in paths
