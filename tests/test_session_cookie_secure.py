"""Regression tests for #139 — session cookie carries the Secure flag.

Before the fix the login cookie set only HttpOnly + SameSite=Lax, so the session id could
travel over cleartext HTTP. After the fix it also sets Secure, driven by
settings.SESSION_COOKIE_SECURE (toggleable off for local HTTP dev).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import bcrypt
import pytest

from app.domain.entities import User


def _make_user() -> User:
    return User(
        id=uuid4(),
        username="admin",
        password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(),
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _login_set_cookie(secure: bool) -> str:
    """Perform a successful login with SESSION_COOKIE_SECURE=`secure`; return the Set-Cookie header."""
    from app.presentation.routes.auth import settings

    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=_make_user())
    mock_redis = AsyncMock()

    with (
        patch("app.presentation.routes.auth._user_repo", mock_repo),
        patch("app.presentation.routes.auth._get_redis", return_value=mock_redis),
        patch.object(settings, "SESSION_COOKIE_SECURE", secure),
    ):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.post(
            "/auth/login",
            data={"username": "admin", "password": "secret"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    set_cookie = "; ".join(resp.headers.get_list("set-cookie"))
    assert "session_id=" in set_cookie
    return set_cookie


def test_login_cookie_is_secure_by_default():
    set_cookie = _login_set_cookie(secure=True).lower()
    assert "secure" in set_cookie
    # The existing hardening stays in place.
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_login_cookie_not_secure_when_disabled():
    set_cookie = _login_set_cookie(secure=False).lower()
    assert "secure" not in set_cookie
    # HttpOnly / SameSite are unconditional.
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
