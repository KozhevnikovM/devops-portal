"""Tests for the Environments browser page (v0.8.0 P3.4, #211)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, EnvironmentBlueprint, User
from app.domain.enums import BookingStatus, ResourceType

_OWNER = UUID(int=0)
_OWNER_ID = str(_OWNER)


def _child(rt=ResourceType.VM, status=BookingStatus.PROVISIONING):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=_OWNER_ID, status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, image_name="Ubuntu",
    )


def _env(children=None, user_id=_OWNER_ID):
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=user_id,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=children if children is not None else [_child()], owner_username="admin",
    )


def _blueprint():
    return EnvironmentBlueprint(id=uuid4(), name="dev-stack", description="ns+web", is_active=True,
                                created_at=datetime.now(timezone.utc), items=[])


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    # An admin whose id == _OWNER so owner checks pass for env rows.
    admin = User(id=_OWNER, username="admin", password_hash="", role="admin",
                 is_active=True, created_at=datetime.now(timezone.utc))
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ns(name="dev1", cluster="prod-cluster"):
    return SimpleNamespace(id=uuid4(), name=name, cluster_name=cluster)


def test_environments_page_renders(client):
    with patch("app.presentation.routes.environments._env_repo") as er, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        er.list_all = AsyncMock(return_value=[_env()])
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[_ns()])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.get("/environments")
    assert resp.status_code == 200
    assert "Order an Environment" in resp.text
    assert "dev-stack" in resp.text
    assert "status-PROVISIONING" in resp.text   # derived aggregate badge
    # Namespace dropdown with the blueprint-default leading option + the available namespace.
    assert "Blueprint default" in resp.text
    assert "dev1 (prod-cluster)" in resp.text


def test_environments_page_empty_blueprints(client):
    with patch("app.presentation.routes.environments._env_repo") as er, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        er.list_all = AsyncMock(return_value=[])
        br.list_active = AsyncMock(return_value=[])
        nr.list_available = AsyncMock(return_value=[])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.get("/environments")
    assert resp.status_code == 200
    assert "No blueprints yet" in resp.text


def test_order_environment_returns_row(client):
    env = _env()
    with patch("app.presentation.routes.environments._order_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.post("/environments", data={"blueprint_name": "dev-stack", "ttl_minutes": "240"})
    assert resp.status_code == 201
    assert f"environment-{env.id}" in resp.text
    assert "dev-stack" in resp.text


def test_order_environment_error_rerenders_form(client):
    from app.domain.exceptions import BlueprintNotFoundError
    with patch("app.presentation.routes.environments._order_use_case") as uc, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        uc.execute = AsyncMock(side_effect=BlueprintNotFoundError("no blueprint"))
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[_ns()])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.post("/environments", data={"blueprint_name": "nope", "ttl_minutes": "240"})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#environment-order-form"
    assert "no blueprint" in resp.text
    # The dropdown survives the error re-render.
    assert "dev1 (prod-cluster)" in resp.text


def test_order_environment_namespace_id_forwarded(client):
    env = _env()
    chosen = uuid4()
    with patch("app.presentation.routes.environments._order_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.post("/environments", data={
            "blueprint_name": "dev-stack", "ttl_minutes": "240", "namespace_id": str(chosen)})
    assert resp.status_code == 201
    assert f"environment-{env.id}" in resp.text
    assert uc.execute.call_args.kwargs["namespace_id"] == chosen


def test_order_environment_bad_blueprint_override_inline_400(client):
    from app.domain.exceptions import EnvironmentItemError
    with patch("app.presentation.routes.environments._order_use_case") as uc, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        uc.execute = AsyncMock(side_effect=EnvironmentItemError("this blueprint has no namespace to choose"))
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[_ns()])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.post("/environments", data={
            "blueprint_name": "vm-only", "ttl_minutes": "240", "namespace_id": str(uuid4())})
    assert resp.status_code == 200   # HTMX inline error re-render
    assert resp.headers.get("HX-Retarget") == "#environment-order-form"
    assert "this blueprint has no namespace to choose" in resp.text


def test_environment_row_poll(client):
    env = _env()
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/environments/{env.id}/row")
    assert resp.status_code == 200
    assert f"environment-{env.id}" in resp.text
    # Non-terminal env keeps polling.
    assert 'hx-get="/environments/' in resp.text


def test_environment_row_403_for_non_owner(client):
    env = _env(user_id="someone-else")
    # current_user (admin id==_OWNER) is admin, so admin can view; test a non-admin non-owner instead.
    from app.main import app
    from app.infrastructure.auth import require_user
    app.dependency_overrides[require_user] = lambda: User(
        id=uuid4(), username="bob", password_hash="", role="user",
        is_active=True, created_at=datetime.now(timezone.utc))
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/environments/{env.id}/row")
    assert resp.status_code == 403


def test_release_environment_returns_row(client):
    released = _env(children=[_child(status=BookingStatus.RELEASING)])
    with patch("app.presentation.routes.environments._release_use_case") as uc:
        uc.execute = AsyncMock(return_value=released)
        resp = client.delete(f"/environments/{released.id}")
    assert resp.status_code == 202
    assert f"environment-{released.id}" in resp.text


def test_release_environment_404(client):
    from app.application.use_cases.release_environment import EnvironmentNotFoundError
    with patch("app.presentation.routes.environments._release_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
        resp = client.delete(f"/environments/{uuid4()}")
    assert resp.status_code == 404


def test_environments_routes_absent_from_schema():
    from app.main import app
    paths = set(TestClient(app).get("/openapi.json").json()["paths"])
    assert "/environments" not in paths
    assert "/environments/{environment_id}/row" not in paths


# ── Held namespace optgroup ────────────────────────────────────────────────────

def test_held_namespaces_optgroup_renders(client):
    """The 'Reuse one of yours' optgroup appears when the user holds a standalone namespace."""
    held = _ns(name="my-ns", cluster="dev-cluster")
    with patch("app.presentation.routes.environments._env_repo") as er, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        er.list_all = AsyncMock(return_value=[])
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[held])
        resp = client.get("/environments")
    assert resp.status_code == 200
    assert "Reuse one of yours" in resp.text
    assert "my-ns (dev-cluster)" in resp.text


def test_held_namespaces_optgroup_absent_when_empty(client):
    """No 'Reuse one of yours' optgroup when the user holds no standalone namespaces."""
    with patch("app.presentation.routes.environments._env_repo") as er, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        er.list_all = AsyncMock(return_value=[])
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[_ns()])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.get("/environments")
    assert resp.status_code == 200
    assert "Reuse one of yours" not in resp.text


def test_held_namespaces_optgroup_in_error_rerender(client):
    """The 'Reuse one of yours' optgroup is preserved after a form error re-render."""
    from app.domain.exceptions import BlueprintNotFoundError
    held = _ns(name="my-ns", cluster="dev-cluster")
    with patch("app.presentation.routes.environments._order_use_case") as uc, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        uc.execute = AsyncMock(side_effect=BlueprintNotFoundError("no blueprint"))
        br.list_active = AsyncMock(return_value=[_blueprint()])
        nr.list_available = AsyncMock(return_value=[])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[held])
        resp = client.post("/environments", data={"blueprint_name": "nope", "ttl_minutes": "240"})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#environment-order-form"
    assert "Reuse one of yours" in resp.text
    assert "my-ns (dev-cluster)" in resp.text
