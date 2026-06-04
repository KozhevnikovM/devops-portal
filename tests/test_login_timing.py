"""Regression tests for #146 — login does the same bcrypt work for missing users.

Before the fix, `not user` short-circuited and bcrypt.checkpw was skipped for unknown usernames,
making the miss path measurably faster (enumeration oracle). After the fix the miss path compares
against a fixed dummy hash, so checkpw runs exactly once regardless.
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
        username="alice",
        password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _client(get_by_username_return):
    from fastapi.testclient import TestClient
    from app.main import app

    mock_repo = MagicMock()
    mock_repo.get_by_username = AsyncMock(return_value=get_by_username_return)
    return mock_repo, TestClient(app)


def test_unknown_user_still_runs_bcrypt_once():
    mock_repo, client = _client(None)
    with (
        patch("app.presentation.routes.auth._user_repo", mock_repo),
        patch("app.presentation.routes.auth.bcrypt.checkpw", wraps=bcrypt.checkpw) as spy,
    ):
        resp = client.post(
            "/auth/login", data={"username": "ghost", "password": "whatever"},
            follow_redirects=False,
        )

    assert resp.status_code == 401
    # The timing-equalizing comparison ran exactly once on the miss path.
    spy.assert_called_once()


def test_wrong_password_returns_401():
    mock_repo, client = _client(_make_user())
    with patch("app.presentation.routes.auth._user_repo", mock_repo):
        resp = client.post(
            "/auth/login", data={"username": "alice", "password": "wrong"},
            follow_redirects=False,
        )
    assert resp.status_code == 401


def test_valid_credentials_succeed():
    mock_repo, client = _client(_make_user())
    mock_redis = AsyncMock()
    with (
        patch("app.presentation.routes.auth._user_repo", mock_repo),
        patch("app.presentation.routes.auth._get_redis", return_value=mock_redis),
    ):
        resp = client.post(
            "/auth/login", data={"username": "alice", "password": "secret"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "session_id" in "; ".join(resp.headers.get_list("set-cookie"))
