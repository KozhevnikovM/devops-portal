import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import bcrypt
import pytest

from app.domain.entities import User


def _make_user(role="user", **kwargs) -> User:
    return User(
        id=kwargs.get("id", uuid4()),
        username=kwargs.get("username", "testuser"),
        password_hash=kwargs.get("password_hash", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()),
        role=role,
        is_active=kwargs.get("is_active", True),
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Login — success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success_sets_cookie_and_redirects():
    user = _make_user(role="admin")

    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=user)
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("app.presentation.routes.auth._user_repo", mock_repo),
        patch("app.presentation.routes.auth._get_redis", return_value=mock_redis),
    ):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/auth/login",
            data={"username": "admin", "password": "secret"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "session_id" in response.cookies


# ---------------------------------------------------------------------------
# Login — wrong password
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_wrong_password_returns_401():
    user = _make_user()

    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=user)

    with patch("app.presentation.routes.auth._user_repo", mock_repo):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/auth/login",
            data={"username": "testuser", "password": "wrongpassword"},
        )

    assert response.status_code == 401
    assert "Invalid" in response.text


# ---------------------------------------------------------------------------
# Login — unknown user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_unknown_user_returns_401():
    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=None)

    with patch("app.presentation.routes.auth._user_repo", mock_repo):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/auth/login",
            data={"username": "nobody", "password": "anything"},
        )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Session resolution — valid session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_user_resolves_from_session_cookie():
    user = _make_user()
    session_payload = json.dumps({"user_id": str(user.id), "username": user.username, "role": user.role})

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=session_payload)

    with patch("app.infrastructure.auth._get_redis", return_value=mock_redis):
        from app.infrastructure.auth import require_user
        from fastapi import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"accept", b"text/html")],
            "query_string": b"",
        }
        request = Request(scope)
        request._cookies = {"session_id": "abc123"}

        mock_user_model = MagicMock()
        mock_user_model.id = user.id
        mock_user_model.username = user.username
        mock_user_model.password_hash = ""
        mock_user_model.role = user.role
        mock_user_model.is_active = True
        mock_user_model.created_at = user.created_at
        mock_user_model.timezone = "UTC"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user_model
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Use the raw function directly
        from app.infrastructure.auth import get_current_user
        result = await get_current_user(request, mock_session)

    assert result is not None
    assert str(result.id) == str(user.id)
    assert result.username == user.username


# ---------------------------------------------------------------------------
# API key resolution — valid key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_resolves_api_key():
    user = _make_user()
    raw_key = "dp_" + "a" * 32
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    mock_repo = MagicMock()
    mock_repo.get_by_key_hash = AsyncMock(return_value=user)

    with patch("app.infrastructure.auth._user_repo", mock_repo):
        from app.infrastructure.auth import get_current_user
        from fastapi import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/bookings",
            "headers": [(b"authorization", f"Bearer {raw_key}".encode())],
            "query_string": b"",
        }
        request = Request(scope)
        mock_session = AsyncMock()

        result = await get_current_user(request, mock_session)

    assert result is not None
    assert result.id == user.id
    mock_repo.get_by_key_hash.assert_called_once_with(mock_session, key_hash)


# ---------------------------------------------------------------------------
# API key resolution — unknown key returns None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_unknown_api_key_returns_none():
    mock_repo = MagicMock()
    mock_repo.get_by_key_hash = AsyncMock(return_value=None)

    with patch("app.infrastructure.auth._user_repo", mock_repo):
        from app.infrastructure.auth import get_current_user
        from fastapi import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/bookings",
            "headers": [(b"authorization", b"Bearer dp_badkey")],
            "query_string": b"",
        }
        request = Request(scope)
        mock_session = AsyncMock()

        result = await get_current_user(request, mock_session)

    assert result is None


# ---------------------------------------------------------------------------
# Unauthenticated browser request → redirect to login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_user_redirects_browser_when_unauthenticated():
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.infrastructure.auth._get_redis", return_value=mock_redis):
        from app.infrastructure.auth import get_current_user, require_user
        from fastapi import HTTPException, Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"accept", b"text/html")],
            "query_string": b"",
        }
        request = Request(scope)
        request._cookies = {}
        mock_session = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await require_user(request, mock_session)

    assert exc_info.value.status_code == 302
    assert exc_info.value.headers["Location"] == "/auth/login"


# ---------------------------------------------------------------------------
# Unauthenticated JSON request → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_user_returns_401_for_json_when_unauthenticated():
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.infrastructure.auth._get_redis", return_value=mock_redis):
        from app.infrastructure.auth import require_user
        from fastapi import HTTPException, Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/bookings",
            "headers": [(b"accept", b"application/json")],
            "query_string": b"",
        }
        request = Request(scope)
        request._cookies = {}
        mock_session = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await require_user(request, mock_session)

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Admin-only gate — non-admin gets 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_admin_raises_403_for_non_admin():
    user = _make_user(role="user")

    with patch("app.infrastructure.auth._user_repo"):
        from app.infrastructure.auth import require_admin
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Admin-only gate — admin passes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_admin_passes_for_admin():
    user = _make_user(role="admin")

    from app.infrastructure.auth import require_admin

    result = await require_admin(user)
    assert result is user
