"""Smoke tests: /api/v1/* routes return valid responses (F-6 acceptance criterion).

Verifies that the versioned canonical paths work end-to-end; the legacy /api/* paths
are tested implicitly by all other existing test files.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from tests.conftest import make_fake_admin


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", [
    "/api/v1/bookings",
    "/api/v1/environments",
    "/api/v1/images",
    "/api/v1/hardware",
    "/api/v1/namespaces",
    "/api/v1/roles",
    "/api/v1/environment-blueprints",
    "/api/v1/static-vms",
])
def test_v1_routes_return_200(admin_client, path):
    """Each /api/v1/* GET endpoint must respond 200 with an empty list."""
    repo_patch = "app.presentation.routes.api"
    bookings_patch = "app.presentation.routes.api_bookings"
    envs_patch = "app.presentation.routes.api_environments"

    with (
        patch(f"{repo_patch}._image_repo") as img,
        patch(f"{repo_patch}._hw_config_repo") as hw,
        patch(f"{repo_patch}._namespace_repo") as ns,
        patch(f"{repo_patch}._role_repo") as roles,
        patch(f"{repo_patch}._blueprint_repo") as blueprints,
        patch(f"{repo_patch}._static_vm_repo") as svms,
        patch(f"{bookings_patch}._repo") as brep,
        patch(f"{envs_patch}._env_repo") as erep,
    ):
        img.list_all = AsyncMock(return_value=[])
        hw.list_all = AsyncMock(return_value=[])
        ns.list_active = AsyncMock(return_value=[])
        roles.list_all = AsyncMock(return_value=[])
        blueprints.list_all = AsyncMock(return_value=[])
        svms.list_active = AsyncMock(return_value=[])
        svms.held_by = AsyncMock(return_value=set())
        brep.list_all = AsyncMock(return_value=[])
        erep.list_all = AsyncMock(return_value=[])
        erep.list_by_user = AsyncMock(return_value=[])

        resp = admin_client.get(path)

    assert resp.status_code == 200, f"{path} → {resp.status_code}: {resp.text}"


def test_v1_paths_appear_in_openapi_schema():
    """The canonical /api/v1/* paths are documented in the OpenAPI schema."""
    schema = TestClient(app).get("/openapi.json").json()
    paths = set(schema["paths"])
    expected = {
        "/api/v1/bookings",
        "/api/v1/environments",
        "/api/v1/images",
        "/api/v1/hardware",
        "/api/v1/roles",
        "/api/v1/environment-blueprints",
    }
    missing = expected - paths
    assert not missing, f"Missing from OpenAPI schema: {missing}"


def test_legacy_api_paths_hidden_from_schema():
    """The unversioned /api/* paths for JSON resources must not appear in the schema."""
    schema = TestClient(app).get("/openapi.json").json()
    paths = set(schema["paths"])
    should_be_hidden = {
        "/api/bookings",
        "/api/environments",
        "/api/images",
        "/api/hardware",
        "/api/roles",
        "/api/environment-blueprints",
    }
    leaked = should_be_hidden & paths
    assert not leaked, f"These /api/* paths should be hidden but appear in schema: {leaked}"
