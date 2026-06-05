from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.entities import Namespace


def _make_ns(**kwargs) -> Namespace:
    return Namespace(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "team-a-dev"),
        cluster_name=kwargs.get("cluster_name", "prod-cluster"),
        api_url=kwargs.get("api_url", "https://api.cluster:6443"),
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

def test_catalog_page_renders_namespace_availability(client):
    ns_free = _make_ns(name="free-ns")
    ns_booked = _make_ns(name="booked-ns")
    with (
        patch("app.presentation.routes.admin._image_repo") as mock_img,
        patch("app.presentation.routes.admin._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.admin._namespace_repo") as mock_ns,
        patch("app.presentation.routes.admin._static_vm_repo") as mock_svm,
    ):
        mock_img.list_all = AsyncMock(return_value=[])
        mock_hw.list_all = AsyncMock(return_value=[])
        mock_ns.list_all = AsyncMock(return_value=[ns_free, ns_booked])
        mock_ns.held_by = AsyncMock(return_value={ns_booked.id: "alice"})
        mock_svm.list_all = AsyncMock(return_value=[])
        mock_svm.held_by = AsyncMock(return_value={})
        resp = client.get("/admin/catalog")

    assert resp.status_code == 200
    assert "free-ns" in resp.text
    assert "Available" in resp.text
    assert "Booked by alice" in resp.text


# ── POST /admin/catalog/namespaces ────────────────────────────────────────────

def test_create_namespace_returns_updated_table(client):
    ns = _make_ns(name="new-ns")
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.create = AsyncMock(return_value=ns)
        mock_ns.list_all = AsyncMock(return_value=[ns])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.post(
            "/admin/catalog/namespaces",
            data={"name": "new-ns", "cluster_name": "prod-cluster", "api_url": ""},
        )

    assert resp.status_code == 200
    assert "new-ns" in resp.text
    assert "namespace-table" in resp.text


def test_create_namespace_duplicate_returns_error_fragment(client):
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/catalog/namespaces",
            data={"name": "team-a-dev", "cluster_name": "prod-cluster", "api_url": ""},
        )

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#namespace-create-error"
    # The clash is on the (name, cluster) pair, so the error names the cluster.
    assert "already exists" in resp.text
    assert "prod-cluster" in resp.text


# ── GET edit / PATCH ──────────────────────────────────────────────────────────

def test_edit_namespace_form_returns_table_with_edit_row(client):
    ns = _make_ns()
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.list_all = AsyncMock(return_value=[ns])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.get(f"/admin/catalog/namespaces/{ns.id}/edit")

    assert resp.status_code == 200
    assert 'hx-patch="/admin/catalog/namespaces/' in resp.text


def test_edit_namespace_form_404_for_missing(client):
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.list_all = AsyncMock(return_value=[])
        resp = client.get(f"/admin/catalog/namespaces/{uuid4()}/edit")

    assert resp.status_code == 404


def test_update_namespace_returns_updated_table(client):
    ns = _make_ns()
    updated = _make_ns(id=ns.id, name="renamed-ns")
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.update = AsyncMock(return_value=updated)
        mock_ns.list_all = AsyncMock(return_value=[updated])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.patch(
            f"/admin/catalog/namespaces/{ns.id}",
            data={"name": "renamed-ns", "cluster_name": "prod-cluster", "api_url": ""},
        )

    assert resp.status_code == 200
    assert "renamed-ns" in resp.text


def test_update_namespace_404_for_missing(client):
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.update = AsyncMock(side_effect=ValueError("not found"))
        resp = client.patch(
            f"/admin/catalog/namespaces/{uuid4()}",
            data={"name": "x", "cluster_name": "c", "api_url": ""},
        )

    assert resp.status_code == 404


# ── activate / deactivate / delete ────────────────────────────────────────────

def test_deactivate_namespace_returns_updated_table(client):
    ns = _make_ns()
    deactivated = _make_ns(id=ns.id, is_active=False)
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.deactivate = AsyncMock()
        mock_ns.list_all = AsyncMock(return_value=[deactivated])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.delete(f"/admin/catalog/namespaces/{ns.id}")

    assert resp.status_code == 200
    assert "inactive" in resp.text


def test_activate_namespace_returns_updated_table(client):
    ns = _make_ns(is_active=False)
    activated = _make_ns(id=ns.id, is_active=True)
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.activate = AsyncMock()
        mock_ns.list_all = AsyncMock(return_value=[activated])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.post(f"/admin/catalog/namespaces/{ns.id}/activate")

    assert resp.status_code == 200
    assert "active" in resp.text


def test_activate_namespace_404_for_missing(client):
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.activate = AsyncMock(side_effect=ValueError("not found"))
        resp = client.post(f"/admin/catalog/namespaces/{uuid4()}/activate")

    assert resp.status_code == 404


def test_hard_delete_namespace_returns_updated_table(client):
    ns = _make_ns(is_active=False)
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.delete = AsyncMock()
        mock_ns.list_all = AsyncMock(return_value=[])
        mock_ns.held_by = AsyncMock(return_value={})
        resp = client.delete(f"/admin/catalog/namespaces/{ns.id}/permanent")

    assert resp.status_code == 200
    assert ns.name not in resp.text


def test_hard_delete_namespace_404_for_missing(client):
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.delete = AsyncMock(side_effect=ValueError("not found"))
        resp = client.delete(f"/admin/catalog/namespaces/{uuid4()}/permanent")

    assert resp.status_code == 404


def test_hard_delete_namespace_blocked_when_bookings_exist(client):
    ns = _make_ns(is_active=False)
    with patch("app.presentation.routes.admin._namespace_repo") as mock_ns:
        mock_ns.delete = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        resp = client.delete(f"/admin/catalog/namespaces/{ns.id}/permanent")

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == f"#namespace-delete-error-{ns.id}"
    assert "Cannot delete" in resp.text


# ── Repository query semantics ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_available_excludes_inactive_and_held():
    from app.infrastructure.repositories.namespace_repo import NamespaceRepository

    repo = NamespaceRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.scalars = lambda: type("S", (), {"all": staticmethod(lambda: [])})()
    session.execute = AsyncMock(return_value=result)

    await repo.list_available(session)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # active-only filter
    assert "is_active" in compiled
    # excludes namespaces referenced by a live booking (subquery over bookings)
    assert "namespace_id" in compiled
    assert "bookings" in compiled
    # released/failed bookings do NOT hold a namespace
    assert "RELEASED" not in compiled
    assert "FAILED" not in compiled
    assert "READY" in compiled


@pytest.mark.asyncio
async def test_held_by_excludes_terminal_bookings():
    from app.infrastructure.repositories.namespace_repo import NamespaceRepository

    repo = NamespaceRepository()
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
