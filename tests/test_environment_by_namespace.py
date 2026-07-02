"""Tests for GET /api/environments/by-namespace/{namespace_name}."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType


def _user(role="user", username="me", uid=None):
    return User(id=uid or uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _ns_child(name="dev1"):
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id="owner", status=BookingStatus.READY,
                   resource_type=ResourceType.NAMESPACE, ttl_minutes=240,
                   expires_at=now + timedelta(minutes=240), created_at=now,
                   namespace_name=name, cluster_name="c1", environment_label="ns")


def _env(owner_id, created_by=None, ns_name="dev1"):
    now = datetime.now(timezone.utc)
    return Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=owner_id,
                       ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                       bookings=[_ns_child(ns_name)], created_by=created_by, owner_username="owner")


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


def test_owner_gets_their_environment_by_namespace():
    owner = _user()
    env = _env(str(owner.id))
    cl, app = _client(owner)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[env])
            resp = cl.get("/api/environments/by-namespace/dev1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(env.id)
    assert body["bookings"][0]["namespace"] == "dev1"
    repo.get_by_namespace.assert_awaited_once_with(
        repo.get_by_namespace.call_args.args[0], "dev1", cluster_name=None
    )


def test_any_authenticated_user_can_read_any_namespace_environment():
    me = _user(username="me")
    env = _env("someone-else", ns_name="dev2")  # owned by another user
    cl, app = _client(me)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[env])
            resp = cl.get("/api/environments/by-namespace/dev2")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["id"] == str(env.id)


def test_unknown_namespace_is_404():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[])
            resp = cl.get("/api/environments/by-namespace/ghost")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_ambiguous_namespace_is_400():
    me = _user()
    cl, app = _client(me)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[_env(str(me.id)), _env(str(me.id))])
            resp = cl.get("/api/environments/by-namespace/dev1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert "ambiguous" in resp.json()["detail"]


def test_cluster_query_param_is_passed_through():
    me = _user()
    cl, app = _client(me)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[_env(str(me.id))])
            resp = cl.get("/api/environments/by-namespace/dev1?cluster=prod")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert repo.get_by_namespace.call_args.kwargs["cluster_name"] == "prod"


def test_admin_gets_any_environment():
    admin = _user(role="admin")
    env = _env("someone-else")
    cl, app = _client(admin)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[env])
            resp = cl.get("/api/environments/by-namespace/dev1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_dispatcher_gets_one_it_dispatched():
    disp = _user(role="dispatcher")
    env = _env("target-owner", created_by=str(disp.id))
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[env])
            resp = cl.get("/api/environments/by-namespace/dev1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


# ── Repo query filters to live environment-child namespace bookings ──────────────
import pytest  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


@pytest.mark.asyncio
async def test_get_by_namespace_query_filters():
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository
    session = MagicMock()
    captured = {}

    async def _execute(stmt):
        captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []  # no matches → [] (skips self.get)
        return result

    session.execute = _execute
    out = await EnvironmentRepository().get_by_namespace(session, "dev1", cluster_name="c1")
    assert out == []
    sql = captured["sql"]
    assert "environment_id" in sql and "namespaces.name" in sql
    assert "resource_type" in sql and "cluster_name" in sql
    assert "status not in" in sql  # terminal statuses excluded
