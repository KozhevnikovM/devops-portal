"""Entry point: run the MCP server over Streamable HTTP.

    python -m mcp_server

Binds to ``MCP_HOST:MCP_PORT`` (default ``0.0.0.0:8765``) and proxies to ``PORTAL_BASE_URL``. Serve
this behind TLS or on a trusted network — clients send their portal API key in the Authorization
header.
"""
from mcp_server.server import mcp


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
