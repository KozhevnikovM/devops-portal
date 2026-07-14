"""Regression tests for #298 — GET /health liveness probe."""
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
