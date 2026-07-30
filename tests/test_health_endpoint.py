"""Regression tests for #298 — GET /health liveness probe, and #371 F-4 — GET /health/ready."""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client():
    from app.main import app
    return TestClient(app)


def test_health_returns_200():
    resp = _client().get("/health")
    assert resp.status_code == 200


def test_health_returns_ok_body():
    resp = _client().get("/health")
    assert resp.json() == {"status": "ok"}


def test_health_requires_no_auth():
    """No session cookie or Authorization header — must still return 200."""
    from app.main import app
    cl = TestClient(app, cookies={})
    resp = cl.get("/health")
    assert resp.status_code == 200


# ── GET /health/ready (#371 F-4) ──────────────────────────────────────────────

def _mock_session_ctx():
    """A mock async context manager whose __aenter__ resolves to a session with .execute()."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=None)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def test_health_ready_returns_200_when_postgres_and_redis_reachable():
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()

    with (
        patch("app.main.AsyncSessionLocal", return_value=_mock_session_ctx()),
        patch("app.main._get_redis", return_value=mock_redis),
    ):
        resp = _client().get("/health/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_returns_503_when_postgres_unreachable():
    with (
        patch("app.main.AsyncSessionLocal", side_effect=Exception("connection refused")),
        patch("app.main._get_redis", return_value=AsyncMock()),
    ):
        resp = _client().get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert "postgres" in body["detail"]


def test_health_ready_returns_503_when_redis_unreachable():
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(side_effect=Exception("connection refused"))
    mock_redis.aclose = AsyncMock()

    with (
        patch("app.main.AsyncSessionLocal", return_value=_mock_session_ctx()),
        patch("app.main._get_redis", return_value=mock_redis),
    ):
        resp = _client().get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert "redis" in body["detail"]


def test_health_ready_returns_503_when_redis_times_out():
    """A hung dependency must not hang the readiness check itself — asyncio.wait_for enforces
    the timeout, which surfaces here as a TimeoutError (treated the same as any other
    unreachable-dependency exception)."""
    import asyncio

    async def _hang_ping():
        raise asyncio.TimeoutError()

    mock_redis = AsyncMock()
    mock_redis.ping = _hang_ping
    mock_redis.aclose = AsyncMock()

    with (
        patch("app.main.AsyncSessionLocal", return_value=_mock_session_ctx()),
        patch("app.main._get_redis", return_value=mock_redis),
    ):
        resp = _client().get("/health/ready")

    assert resp.status_code == 503
