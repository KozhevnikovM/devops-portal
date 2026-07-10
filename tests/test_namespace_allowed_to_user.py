"""Tests for GET /api/environments/by-namespace/{namespace_name}/allowed-to-user."""
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, Namespace, User
from app.domain.enums import BookingStatus, ResourceType


def _user(role="user", username="caller"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _ns_booking(namespace_id: uuid4 = None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=str(uuid4()), status=BookingStatus.READY,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        resource_type=ResourceType.NAMESPACE,
        namespace_id=namespace_id or uuid4(),
    )


def _env(owner_username, bookings=None):
    now = datetime.now(timezone.utc)
    return Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=str(uuid4()),
                       ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                       bookings=bookings or [], owner_username=owner_username)


def _namespace(name="dev1", ns_id=None):
    return Namespace(id=ns_id or uuid4(), name=name, cluster_name="prod",
                     api_url=None, is_active=True, created_at=datetime.now(timezone.utc))


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


def _call(envs, user="john", ns_repo_matches=None):
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as env_repo, \
             patch("app.presentation.routes.api_environments._namespace_repo") as ns_repo:
            env_repo.get_by_namespace = AsyncMock(return_value=envs)
            ns_repo.get_by_name = AsyncMock(return_value=ns_repo_matches or [])
            ns_repo.get_by_name_and_cluster = AsyncMock(return_value=None)
            return cl.get(f"/api/environments/by-namespace/dev1/allowed-to-user?user={user}")
    finally:
        app.dependency_overrides.clear()


def test_owner_match_is_202():
    ns_id = uuid4()
    env_id_holder = []
    nb = _ns_booking(namespace_id=ns_id)
    env = _env(owner_username="john", bookings=[nb])
    env_id_holder.append(env.id)
    resp = _call([env], user="john")
    assert resp.status_code == 202
    body = resp.json()
    assert body["match"] is True and body["vacant"] is False
    assert body["user"] == "john" and body["namespace"] == "dev1"
    assert body["namespace_id"] == str(ns_id)
    assert body["environment_id"] == str(env.id)


def test_owner_match_no_namespace_booking_returns_null_namespace_id():
    env = _env(owner_username="john", bookings=[])
    resp = _call([env], user="john")
    assert resp.status_code == 202
    body = resp.json()
    assert body["namespace_id"] is None
    assert body["environment_id"] == str(env.id)


def test_owned_by_other_is_423_without_owner_name():
    resp = _call([_env(owner_username="marry")], user="john")
    assert resp.status_code == 423
    assert "marry" not in resp.text  # real owner never disclosed


def test_vacant_namespace_is_202_with_catalog_hit():
    ns = _namespace()
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as env_repo, \
             patch("app.presentation.routes.api_environments._namespace_repo") as ns_repo:
            env_repo.get_by_namespace = AsyncMock(return_value=[])
            ns_repo.get_by_name = AsyncMock(return_value=[ns])
            resp = cl.get("/api/environments/by-namespace/dev1/allowed-to-user?user=john")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    body = resp.json()
    assert body["match"] is False and body["vacant"] is True
    assert body["namespace_id"] == str(ns.id)
    assert body["environment_id"] is None


def test_vacant_namespace_id_null_when_not_in_catalog():
    resp = _call([], user="john", ns_repo_matches=[])
    assert resp.status_code == 202
    body = resp.json()
    assert body["match"] is False and body["vacant"] is True
    assert body["namespace_id"] is None
    assert body["environment_id"] is None


def test_vacant_ambiguous_across_clusters_is_400():
    ns1, ns2 = _namespace(), _namespace()
    resp = _call([], user="john", ns_repo_matches=[ns1, ns2])
    assert resp.status_code == 400
    assert "ambiguous" in resp.json()["detail"]


def test_vacant_with_cluster_param_uses_get_by_name_and_cluster():
    ns = _namespace()
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as env_repo, \
             patch("app.presentation.routes.api_environments._namespace_repo") as ns_repo:
            env_repo.get_by_namespace = AsyncMock(return_value=[])
            ns_repo.get_by_name_and_cluster = AsyncMock(return_value=ns)
            resp = cl.get("/api/environments/by-namespace/dev1/allowed-to-user?user=john&cluster=prod")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    body = resp.json()
    assert body["namespace_id"] == str(ns.id)
    assert body["environment_id"] is None
    ns_repo.get_by_name_and_cluster.assert_awaited_once_with(ANY, "dev1", "prod")


def test_ambiguous_namespace_is_400():
    resp = _call([_env("john"), _env("john")], user="john")
    assert resp.status_code == 400
    assert "ambiguous" in resp.json()["detail"]


def test_missing_user_param_is_422():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[_env("john")])
            resp = cl.get("/api/environments/by-namespace/dev1/allowed-to-user")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_cluster_param_passed_through():
    nb = _ns_booking()
    env = _env(owner_username="john", bookings=[nb])
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as env_repo, \
             patch("app.presentation.routes.api_environments._namespace_repo"):
            env_repo.get_by_namespace = AsyncMock(return_value=[env])
            resp = cl.get("/api/environments/by-namespace/dev1/allowed-to-user?user=john&cluster=prod")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    assert env_repo.get_by_namespace.call_args.kwargs["cluster_name"] == "prod"
