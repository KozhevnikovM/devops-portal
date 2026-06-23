"""MCP server for the DevOps Portal.

A thin, stateless Model Context Protocol server over the portal's public ``/api/*`` JSON API,
served over Streamable HTTP. Agentic coding tools (opencode, qwen-cli, Claude Code, …) connect to it
and drive the portal's self-service operations as MCP tools.

It holds no credentials: every tool forwards the caller's portal API key (the inbound
``Authorization: Bearer dp_<key>`` header) to the portal, so the portal's own auth, quota, and
permission rules apply unchanged. See ``docs/features/mcp-server.md``.
"""
