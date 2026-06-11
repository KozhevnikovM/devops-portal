"""Tests for GET /api/environments/by-namespace/{namespace_name}/allowed-to-user."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.entities import Environment, User


def _user(role="user", username="caller"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _env(owner_username):
    now = datetime.now(timezone.utc)
    return Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=str(uuid4()),
                       ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                       bookings=[], owner_username=owner_username)


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


def _call(envs, user="john"):
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=envs)
            return cl.get(f"/api/environments/by-namespace/dev1/allowed-to-user?user={user}")
    finally:
        app.dependency_overrides.clear()


def test_owner_match_is_202():
    resp = _call([_env(owner_username="john")], user="john")
    assert resp.status_code == 202
    body = resp.json()
    assert body["match"] is True and body["user"] == "john" and body["namespace"] == "dev1"


def test_owned_by_other_is_423_without_owner_name():
    resp = _call([_env(owner_username="marry")], user="john")
    assert resp.status_code == 423
    assert "marry" not in resp.text  # real owner never disclosed


def test_no_environment_is_423():
    resp = _call([], user="john")
    assert resp.status_code == 423


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
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as repo:
            repo.get_by_namespace = AsyncMock(return_value=[_env("john")])
            resp = cl.get("/api/environments/by-namespace/dev1/allowed-to-user?user=john&cluster=prod")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 202
    assert repo.get_by_namespace.call_args.kwargs["cluster_name"] == "prod"
