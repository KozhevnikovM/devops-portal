"""Tests for the MCP server (mcp_server/).

Covers the three layers of the thin proxy:
  - PortalClient: right URL/verb/body, 2xx → data, non-2xx → PortalError, unreachable → PortalError.
  - Tools: forward the caller's bearer token, shape args into the portal call, and translate a
    PortalError into a ToolError carrying status + detail.
  - Token forwarding: a request with no Authorization header yields a ToolError and makes no call.
"""
import types

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import server
from mcp_server.portal_client import PortalClient, PortalError


# ── PortalClient ──────────────────────────────────────────────────────────────────


def _client_with(handler) -> PortalClient:
    """A PortalClient wired to a mock transport running `handler(request) -> Response`."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return PortalClient("http://portal:8000", "dp_secret", client=http)


async def test_client_forwards_token_and_parses_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=[{"id": "1"}])

    result = await _client_with(handler).list_bookings()

    assert result == [{"id": "1"}]
    assert seen["auth"] == "Bearer dp_secret"
    assert seen["url"] == "http://portal:8000/api/bookings"
    assert seen["method"] == "GET"


async def test_client_create_booking_posts_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return httpx.Response(201, json={"id": "b1", "status": "PENDING"})

    body = {"resource_type": "VM", "ttl_minutes": 60, "image_name": "Ubuntu 22.04"}
    result = await _client_with(handler).create_booking(body)

    assert result["status"] == "PENDING"
    assert seen["method"] == "POST"
    assert seen["body"] == body


async def test_client_extend_and_find_build_right_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"ok": True})

    client = _client_with(handler)
    await client.extend_booking("abc", 30)
    assert seen["url"] == "http://portal:8000/api/bookings/abc/extend"
    assert seen["method"] == "PUT"

    await client.find_environment_by_namespace("dev1", cluster="prod")
    assert seen["url"] == "http://portal:8000/api/environments/by-namespace/dev1?cluster=prod"


async def test_client_204_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    assert await _client_with(handler).release_booking("b1") is None


async def test_client_non_2xx_raises_portal_error_with_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Quota exceeded: CPU (18/16 cores)"})

    with pytest.raises(PortalError) as exc:
        await _client_with(handler).create_booking({"ttl_minutes": 1})

    assert exc.value.status == 409
    assert exc.value.detail == "Quota exceeded: CPU (18/16 cores)"
    assert "409" in str(exc.value)


async def test_client_unreachable_raises_portal_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(PortalError) as exc:
        await _client_with(handler).list_bookings()

    assert exc.value.status is None
    assert "unreachable" in exc.value.detail


# ── Tools (token forwarding + arg shaping + error translation) ─────────────────────


def _ctx(auth: str | None):
    """A minimal Context double exposing request.headers like the HTTP transport does."""
    headers = {"Authorization": auth} if auth is not None else {}
    request = types.SimpleNamespace(headers=headers)
    request_context = types.SimpleNamespace(request=request)
    return types.SimpleNamespace(request_context=request_context)


class _FakePortalClient:
    """Stand-in for PortalClient that records construction + the call made."""

    last = None

    def __init__(self, base_url, token, **kwargs):
        self.base_url = base_url
        self.token = token
        _FakePortalClient.last = self
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    async def list_bookings(self):
        self._record("list_bookings")
        return [{"id": "b1"}]

    async def create_booking(self, body):
        self._record("create_booking", body)
        return {"id": "b1", "status": "PENDING"}

    async def list_vm_images(self):
        self._record("list_vm_images")
        return [{"name": "Ubuntu 22.04"}]


@pytest.fixture
def fake_client(monkeypatch):
    _FakePortalClient.last = None
    monkeypatch.setattr(server, "PortalClient", _FakePortalClient)
    return _FakePortalClient


async def test_tool_forwards_bearer_token(fake_client):
    result = await server.list_bookings(_ctx("Bearer dp_userkey"))

    assert result == [{"id": "b1"}]
    assert fake_client.last.token == "dp_userkey"
    assert fake_client.last.calls == [("list_bookings", ())]


async def test_create_booking_tool_shapes_body(fake_client):
    await server.create_booking(
        _ctx("Bearer dp_userkey"),
        ttl_minutes=60,
        image_name="Ubuntu 22.04",
        hw_config_name="medium",
    )

    name, (body,) = fake_client.last.calls[0]
    assert name == "create_booking"
    assert body == {
        "resource_type": "VM",
        "ttl_minutes": 60,
        "image_name": "Ubuntu 22.04",
        "hw_config_name": "medium",
    }
    # None-valued optionals are dropped, not sent as nulls.
    assert "namespace_id" not in body
    assert "on_behalf_of" not in body


async def test_tool_without_token_errors_and_makes_no_call(fake_client):
    with pytest.raises(ToolError) as exc:
        await server.list_bookings(_ctx(None))

    assert "API key" in str(exc.value)
    assert fake_client.last is None  # PortalClient never constructed


async def test_tool_translates_portal_error_to_tool_error(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def list_vm_images(self):
            raise PortalError(403, "Admin access required")

    monkeypatch.setattr(server, "PortalClient", _Boom)

    with pytest.raises(ToolError) as exc:
        await server.list_vm_images(_ctx("Bearer dp_userkey"))

    assert "403" in str(exc.value)
    assert "Admin access required" in str(exc.value)
