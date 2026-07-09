"""Tests for change/reset password feature (#290)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import bcrypt
import pytest

from tests.conftest import make_fake_admin, make_fake_user


# ── UserRepository.update_password ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_password_sets_new_hash():
    from app.infrastructure.repositories.user_repo import UserRepository
    from app.infrastructure.database.models import UserModel

    repo = UserRepository()
    user_id = uuid4()
    model = MagicMock(spec=UserModel)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=model)))

    await repo.update_password(session, user_id, "new_hash_value")

    assert model.password_hash == "new_hash_value"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_password_noop_when_user_missing():
    from app.infrastructure.repositories.user_repo import UserRepository

    repo = UserRepository()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    await repo.update_password(session, uuid4(), "hash")

    session.commit.assert_not_awaited()


# ── _invalidate_user_sessions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalidate_deletes_all_sessions():
    from app.presentation.routes.auth import _invalidate_user_sessions

    r = AsyncMock()
    r.smembers = AsyncMock(return_value={"sid1", "sid2"})

    await _invalidate_user_sessions(r, "uid-123")

    r.smembers.assert_awaited_once_with("user_sessions:uid-123")
    # Both session keys deleted
    deleted_keys = r.delete.call_args_list
    all_args = [arg for call in deleted_keys for arg in call.args]
    assert "session:sid1" in all_args
    assert "session:sid2" in all_args
    # Set itself deleted
    assert any("user_sessions:uid-123" in str(call) for call in deleted_keys)


@pytest.mark.asyncio
async def test_invalidate_keeps_current_session():
    from app.presentation.routes.auth import _invalidate_user_sessions

    r = AsyncMock()
    r.smembers = AsyncMock(return_value={"keep-me", "delete-me"})

    await _invalidate_user_sessions(r, "uid-123", keep_session_id="keep-me")

    # delete-me should be deleted, keep-me should not be in any session delete call
    all_delete_args = [a for call in r.delete.call_args_list for a in call.args]
    assert "session:delete-me" in all_delete_args
    assert "session:keep-me" not in all_delete_args
    # kept session re-added to set
    r.sadd.assert_awaited_once_with("user_sessions:uid-123", "keep-me")


@pytest.mark.asyncio
async def test_invalidate_noop_when_no_sessions():
    from app.presentation.routes.auth import _invalidate_user_sessions

    r = AsyncMock()
    r.smembers = AsyncMock(return_value=set())

    await _invalidate_user_sessions(r, "uid-123")

    r.sadd.assert_not_awaited()


# ── Login / logout session tracking ──────────────────────────────────────────

@pytest.fixture
def auth_client():
    from app.main import app
    from app.infrastructure.database.session import get_async_session
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_login_adds_to_user_sessions_set(auth_client):
    fake_user = make_fake_user()
    pw_hash = bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode()
    fake_user = fake_user.__class__(
        **{**fake_user.__dict__, "password_hash": pw_hash}
    )

    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(return_value=set())

    with patch("app.presentation.routes.auth._user_repo") as ur, \
         patch("app.presentation.routes.auth._get_redis", return_value=redis_mock):
        ur.get_by_username = AsyncMock(return_value=fake_user)
        auth_client.post("/auth/login", data={"username": "test", "password": "password123"})

    redis_mock.sadd.assert_awaited_once()
    call_args = redis_mock.sadd.call_args
    assert call_args.args[0] == f"user_sessions:{fake_user.id}"


def test_logout_removes_from_user_sessions_set(auth_client):
    session_id = "test-session-id"
    user_id = str(uuid4())

    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=json.dumps({"user_id": user_id}))

    with patch("app.presentation.routes.auth._get_redis", return_value=redis_mock):
        auth_client.post(
            "/auth/logout",
            cookies={"session_id": session_id},
        )

    redis_mock.srem.assert_awaited_once_with(f"user_sessions:{user_id}", session_id)


# ── POST /profile/password ────────────────────────────────────────────────────

@pytest.fixture
def profile_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from fastapi.testclient import TestClient

    fake_user = make_fake_user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


def test_change_password_success(profile_client):
    client, fake_user = profile_client
    pw = "old-password"
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    db_user = fake_user.__class__(**{**fake_user.__dict__, "password_hash": pw_hash})

    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(return_value=set())

    with patch("app.presentation.routes.auth._user_repo") as ur, \
         patch("app.presentation.routes.auth._get_redis", return_value=redis_mock):
        ur.get = AsyncMock(return_value=db_user)
        ur.update_password = AsyncMock()
        resp = client.post(
            "/profile/password",
            data={"current_password": pw, "new_password": "new-password-123"},
        )

    assert resp.status_code == 200
    assert "Password changed" in resp.text
    ur.update_password.assert_awaited_once()


def test_change_password_wrong_current(profile_client):
    client, fake_user = profile_client
    pw_hash = bcrypt.hashpw(b"real-password", bcrypt.gensalt()).decode()
    db_user = fake_user.__class__(**{**fake_user.__dict__, "password_hash": pw_hash})

    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.get = AsyncMock(return_value=db_user)
        ur.update_password = AsyncMock()
        resp = client.post(
            "/profile/password",
            data={"current_password": "wrong-password", "new_password": "new-password-123"},
        )

    assert resp.status_code == 200
    assert "incorrect" in resp.text
    ur.update_password.assert_not_awaited()


def test_change_password_too_short(profile_client):
    client, _ = profile_client
    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.get = AsyncMock()
        resp = client.post(
            "/profile/password",
            data={"current_password": "anything", "new_password": "short"},
        )

    assert resp.status_code == 200
    assert "8 characters" in resp.text
    ur.get.assert_not_awaited()


# ── POST /api/users/{user_id}/password ───────────────────────────────────────

@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from fastapi.testclient import TestClient

    fake_admin = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_admin] = lambda: fake_admin
    yield TestClient(app), fake_admin
    app.dependency_overrides.clear()


def test_admin_reset_password_success(admin_client):
    client, _ = admin_client
    target_id = uuid4()
    target_user = make_fake_user().__class__(
        **{**make_fake_user().__dict__, "id": target_id}
    )

    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(return_value=set())

    with patch("app.presentation.routes.auth._user_repo") as ur, \
         patch("app.presentation.routes.auth._get_redis", return_value=redis_mock):
        ur.get = AsyncMock(return_value=target_user)
        ur.update_password = AsyncMock()
        resp = client.post(
            f"/api/users/{target_id}/password",
            json={"new_password": "brand-new-pw-123"},
        )

    assert resp.status_code == 204
    ur.update_password.assert_awaited_once()


def test_admin_reset_password_too_short(admin_client):
    client, _ = admin_client
    resp = client.post(
        f"/api/users/{uuid4()}/password",
        json={"new_password": "short"},
    )
    assert resp.status_code == 422
    assert "8 characters" in resp.json()["detail"]


def test_admin_reset_password_user_not_found(admin_client):
    client, _ = admin_client
    with patch("app.presentation.routes.auth._user_repo") as ur:
        ur.get = AsyncMock(return_value=None)
        resp = client.post(
            f"/api/users/{uuid4()}/password",
            json={"new_password": "valid-password-123"},
        )
    assert resp.status_code == 404


def test_admin_reset_invalidates_all_sessions(admin_client):
    client, _ = admin_client
    target_id = uuid4()
    target_user = make_fake_user().__class__(
        **{**make_fake_user().__dict__, "id": target_id}
    )

    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(return_value={"session-a", "session-b"})

    with patch("app.presentation.routes.auth._user_repo") as ur, \
         patch("app.presentation.routes.auth._get_redis", return_value=redis_mock):
        ur.get = AsyncMock(return_value=target_user)
        ur.update_password = AsyncMock()
        client.post(
            f"/api/users/{target_id}/password",
            json={"new_password": "brand-new-pw-123"},
        )

    # Both sessions deleted, set not rebuilt (no keep_session_id)
    all_delete_args = [a for call in redis_mock.delete.call_args_list for a in call.args]
    assert "session:session-a" in all_delete_args
    assert "session:session-b" in all_delete_args
    redis_mock.sadd.assert_not_awaited()


def test_non_admin_cannot_reset_password():
    from app.main import app
    from app.infrastructure.database.session import get_async_session
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    # No session cookie → require_user returns 401 when Accept is application/json
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/users/{uuid4()}/password",
        json={"new_password": "valid-password-123"},
        headers={"Accept": "application/json"},
    )
    app.dependency_overrides.clear()
    assert resp.status_code in (401, 403)
