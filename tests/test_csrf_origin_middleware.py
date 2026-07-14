"""Regression tests for #296 — CSRFOriginMiddleware blocks mismatched Origins."""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.presentation.middleware.csrf_origin import CSRFOriginMiddleware

_BASE_URL = "https://portal.example.com"


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _make_client(base_url: str = _BASE_URL) -> TestClient:
    app = Starlette(routes=[
        Route("/action", _ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ])
    app.add_middleware(CSRFOriginMiddleware, base_url=base_url)
    return TestClient(app, raise_server_exceptions=True)


# ── GET is always allowed ─────────────────────────────────────────────────────

def test_get_with_mismatched_origin_is_allowed():
    cl = _make_client()
    resp = cl.get("/action", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200


# ── Matching Origin ───────────────────────────────────────────────────────────

def test_post_with_matching_origin_allowed():
    cl = _make_client()
    resp = cl.post("/action", headers={"Origin": _BASE_URL})
    assert resp.status_code == 200


def test_post_with_matching_origin_trailing_slash_allowed():
    cl = _make_client()
    resp = cl.post("/action", headers={"Origin": _BASE_URL + "/"})
    assert resp.status_code == 200


# ── Mismatched Origin ─────────────────────────────────────────────────────────

def test_post_with_foreign_origin_rejected():
    cl = _make_client()
    resp = cl.post("/action", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_put_with_foreign_origin_rejected():
    cl = _make_client()
    resp = cl.put("/action", headers={"Origin": "https://attacker.io"})
    assert resp.status_code == 403


def test_delete_with_foreign_origin_rejected():
    cl = _make_client()
    resp = cl.delete("/action", headers={"Origin": "https://attacker.io"})
    assert resp.status_code == 403


# ── Absent Origin ─────────────────────────────────────────────────────────────

def test_post_without_origin_header_allowed():
    """Same-origin browser/API requests often omit Origin; must not be blocked."""
    cl = _make_client()
    resp = cl.post("/action")
    assert resp.status_code == 200


def test_delete_without_origin_header_allowed():
    cl = _make_client()
    resp = cl.delete("/action")
    assert resp.status_code == 200


# ── Referer fallback ──────────────────────────────────────────────────────────

def test_post_with_matching_referer_allowed():
    cl = _make_client()
    resp = cl.post("/action", headers={"Referer": _BASE_URL + "/bookings"})
    assert resp.status_code == 200


def test_post_with_foreign_referer_rejected():
    cl = _make_client()
    resp = cl.post("/action", headers={"Referer": "https://evil.example.com/page"})
    assert resp.status_code == 403


# ── BASE_URL trailing-slash normalisation ─────────────────────────────────────

def test_base_url_with_trailing_slash_still_matches():
    cl = _make_client(base_url=_BASE_URL + "/")
    resp = cl.post("/action", headers={"Origin": _BASE_URL})
    assert resp.status_code == 200
