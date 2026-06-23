"""End-to-end smoke test: the real MCP SDK client over the Streamable HTTP transport.

Runs the actual FastMCP app in a background uvicorn thread, connects with the SDK's
``streamablehttp_client``, and drives ``create_booking`` → ``list_bookings``. The portal itself is
stubbed (PortalClient is monkeypatched), so this exercises the full MCP wiring — transport, tool
dispatch, and **token forwarding** (the bearer header set on the client must reach the stub) —
without needing a running portal.
"""
import json
import socket
import threading
import time

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp_server import server


class _StubPortalClient:
    """Captures the forwarded token and serves canned data for the two tools used here."""

    tokens: list[str] = []

    def __init__(self, base_url, token, **kwargs):
        _StubPortalClient.tokens.append(token)

    async def create_booking(self, body):
        return {"id": "b1", "status": "PENDING", **body}

    async def list_bookings(self):
        return [{"id": "b1", "status": "READY"}]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(monkeypatch):
    import uvicorn

    monkeypatch.setattr(server, "PortalClient", _StubPortalClient)
    _StubPortalClient.tokens = []

    port = _free_port()
    app = server.mcp.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()

    # Wait for the listener to accept connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("MCP server did not start")

    yield f"http://127.0.0.1:{port}/mcp"

    uv.should_exit = True
    thread.join(timeout=5)


async def test_e2e_create_then_list_over_http(running_server):
    headers = {"Authorization": "Bearer dp_e2ekey"}
    async with streamablehttp_client(running_server, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"create_booking", "list_bookings"}.issubset(tools)

            created = await session.call_tool(
                "create_booking",
                {"ttl_minutes": 60, "image_name": "Ubuntu 22.04", "hw_config_name": "medium"},
            )
            created_body = json.loads(created.content[0].text)
            assert created_body["status"] == "PENDING"
            # The tool shaped the args into the documented booking body.
            assert created_body["resource_type"] == "VM"
            assert created_body["image_name"] == "Ubuntu 22.04"

            listed = await session.call_tool("list_bookings", {})
            # A list return is emitted as one content item per row; the stub returns a single row.
            assert json.loads(listed.content[0].text)["id"] == "b1"

    # The bearer token from the MCP client reached the portal stub (token forwarding).
    assert _StubPortalClient.tokens
    assert all(tok == "dp_e2ekey" for tok in _StubPortalClient.tokens)
