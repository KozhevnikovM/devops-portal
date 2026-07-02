"""Regression tests for GET /api/namespaces (issue #268)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Namespace, User


def _user(role="user"):
    return User(id=uuid4(), username="me", password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _ns(name="dev1", cluster="prod"):
    return Namespace(id=uuid4(), name=name, cluster_name=cluster,
                     api_url="https://k8s.example.com", is_active=True,
                     created_at=datetime.now(timezone.utc))


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app), app


# ── Default filter=active ─────────────────────────────────────────────────────

def test_list_namespaces_default_calls_list_active():
    cl, app = _client(_user())
    ns = _ns()
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_active = AsyncMock(return_value=[ns])
            resp = cl.get("/api/namespaces")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_active.assert_awaited_once()
    assert resp.json()[0]["name"] == "dev1"


def test_list_namespaces_filter_active_explicit():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_active = AsyncMock(return_value=[])
            resp = cl.get("/api/namespaces?filter=active")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_active.assert_awaited_once()


# ── filter=available ──────────────────────────────────────────────────────────

def test_list_namespaces_filter_available():
    cl, app = _client(_user())
    ns = _ns("free1")
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_available = AsyncMock(return_value=[ns])
            resp = cl.get("/api/namespaces?filter=available")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_available.assert_awaited_once()
    assert resp.json()[0]["name"] == "free1"


# ── username filter ───────────────────────────────────────────────────────────

def test_list_namespaces_username_filter():
    cl, app = _client(_user())
    ns = _ns("alice-ns")
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_held_by_username = AsyncMock(return_value=[ns])
            resp = cl.get("/api/namespaces?username=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_held_by_username.assert_awaited_once_with(
        repo.list_held_by_username.call_args.args[0], "alice"
    )
    assert resp.json()[0]["name"] == "alice-ns"


# ── not_username filter ───────────────────────────────────────────────────────

def test_list_namespaces_not_username_filter():
    cl, app = _client(_user())
    ns = _ns("other-ns")
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_active_not_held_by_username = AsyncMock(return_value=[ns])
            resp = cl.get("/api/namespaces?not_username=alice")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    repo.list_active_not_held_by_username.assert_awaited_once_with(
        repo.list_active_not_held_by_username.call_args.args[0], "alice"
    )
    assert resp.json()[0]["name"] == "other-ns"


# ── Error cases ───────────────────────────────────────────────────────────────

def test_invalid_filter_returns_400():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api._namespace_repo"):
            resp = cl.get("/api/namespaces?filter=bogus")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert "invalid filter" in resp.json()["detail"]


def test_username_and_not_username_together_returns_400():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api._namespace_repo"):
            resp = cl.get("/api/namespaces?username=alice&not_username=bob")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert "mutually exclusive" in resp.json()["detail"]


def test_unauthenticated_returns_401():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = _deny
    cl = TestClient(app, raise_server_exceptions=False)
    try:
        resp = cl.get("/api/namespaces")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 401


# ── Response shape ────────────────────────────────────────────────────────────

def test_response_shape():
    cl, app = _client(_user())
    ns = _ns("shape-test", cluster="c1")
    try:
        with patch("app.presentation.routes.api._namespace_repo") as repo:
            repo.list_active = AsyncMock(return_value=[ns])
            resp = cl.get("/api/namespaces")
    finally:
        app.dependency_overrides.clear()
    item = resp.json()[0]
    assert set(item.keys()) == {"id", "name", "cluster_name", "api_url", "is_active", "created_at"}
    assert item["cluster_name"] == "c1"
    assert item["is_active"] is True
