# Feature: MCP server for the DevOps Portal

## Goal

Expose the portal's self-service operations as an **MCP (Model Context Protocol) server** so that
agentic coding tools — opencode, qwen-cli, Claude Code, etc., including ones driven by self-hosted
models (e.g. Qwen Coder 27B) — can discover the catalog and manage bookings/environments
conversationally instead of hand-rolling `curl` calls against the JSON API.

The server is a **thin, stateless proxy** over the existing `/api/*` JSON endpoints. It adds no new
business logic and no new persistence — every tool maps to one HTTP call against the portal, reusing
the portal's own auth, quota, and permission rules. If the portal rejects a call (`401/403/409/…`),
the tool surfaces that error verbatim.

Non-goals: admin catalog/user mutation (no image/hardware/namespace/role/user/quota writes, no
permanent deletes), and the HTML/HTMX browser routes. Those stay out of the tool surface — an LLM
should not be able to deactivate catalog entries or delete users. Read-only *discovery* of the
catalog is included because ordering by name needs it.

## Decisions (agreed up front)

- **Scope:** full self-service lifecycle — read-only discovery + booking/environment
  create/list/extend/release/audit. No admin writes.
- **Transport:** Streamable HTTP (the MCP SDK's `streamable-http` transport, with SSE responses).
  The server runs as a long-lived HTTP endpoint, not a per-client subprocess.
- **Packaging:** in-repo. New top-level package `mcp_server/`, sharing the project venv, started with
  `python -m mcp_server`.

## Domain model & layering

No domain change. This is a new **presentation/adapter surface** that sits *outside* the portal
process and talks to it over HTTP, exactly like Jenkins or a `curl` script does. It does **not**
import `app.application` / `app.infrastructure` / `app.domain` — it only knows the public JSON
contract. That keeps the one-way dependency rule intact (it's another external client, not an inner
layer) and means the MCP server can even run as a separate container pointed at a remote portal.

```
mcp_server/  →  HTTP  →  portal /api/*   (no Python import coupling)
```

## Authentication — token forwarding (the key design point)

HTTP transport means many users may hit one MCP endpoint, so the server must **not** hold a single
shared API key. Instead it is **stateless and per-request authenticated by forwarding the caller's
portal API key**:

1. The MCP client is configured with the user's portal key, sent as `Authorization: Bearer dp_<key>`
   on every MCP HTTP request (standard MCP client header config).
2. Each tool reads that incoming `Authorization` header from the MCP request context and forwards
   the **same** header to the portal `/api/*` call.
3. The portal authenticates it exactly as today (`require_user` → `get_by_key_hash`). The booking is
   owned by, and counts against the quota of, whoever owns that key. Dispatcher keys keep their
   `on_behalf_of` power.

So the MCP server stores **zero** credentials and inherits the portal's entire authz model for free.
A request with no/!invalid bearer token → the tool returns the portal's `401` as an error result.
Config the server *does* need: `PORTAL_BASE_URL` (default `http://localhost:8000`) and the
HTTP bind host/port (`MCP_HOST`/`MCP_PORT`, default `0.0.0.0:8765`).

> **Transport security note.** Because keys ride in the `Authorization` header, the endpoint should be
> served over TLS (or kept on a trusted/loopback network) in any real deployment — same posture as the
> portal's own API. Documented in the admin guide.

## Tools

Each tool is one portal call. Names are verb-first and snake_case; descriptions and JSON-schema'd
args come straight from `docs/api-reference.md` so a model can pick the right one.

**Discovery (read-only):**

| Tool | Portal call |
|------|-------------|
| `list_vm_images` | `GET /api/images` |
| `list_hardware_configs` | `GET /api/hardware` |
| `list_static_vms` | `GET /api/static-vms` |
| `list_roles` | `GET /api/roles` |
| `list_blueprints` | `GET /api/environment-blueprints` |

**Bookings:**

| Tool | Portal call |
|------|-------------|
| `list_bookings` | `GET /api/bookings` |
| `create_booking` | `POST /api/bookings` |
| `extend_booking` | `PUT /api/bookings/{id}/extend` |
| `release_booking` | `DELETE /api/bookings/{id}` |
| `get_booking_audit` | `GET /api/bookings/{id}/audit` |

`create_booking` takes the full documented body (`resource_type`, `ttl_minutes`, `image_name`/
`hw_config_name` or `*_id`, `namespace_name`+`cluster_name`, `static_vm_name`, `startup_script`,
`roles`, `on_behalf_of`). We pass the user's fields straight through; the portal validates. Ordering
**by name** is the documented happy path, so the tool description steers models to names (discoverable
via the list tools) over UUIDs.

**Environments:**

| Tool | Portal call |
|------|-------------|
| `list_environments` | `GET /api/environments` |
| `get_environment` | `GET /api/environments/{id}` |
| `create_environment` | `POST /api/environments` |
| `release_environment` | `DELETE /api/environments/{id}` |
| `find_environment_by_namespace` | `GET /api/environments/by-namespace/{name}?cluster=` |

## Behaviour & edge cases

- **Errors pass through.** Non-2xx from the portal becomes an MCP tool error whose message includes
  the status code and the portal's `detail` (e.g. `409 Quota exceeded: CPU (18/16 cores)`), so the
  model can react (pick a smaller hw config, release something, retry). We do **not** swallow or
  reinterpret them.
- **Async, non-terminal statuses are not awaited.** `create_booking` for a `VM` returns immediately
  with `PENDING`/`PROVISIONING`; a pooled "any available" against an empty pool returns `QUEUED`.
  The model polls with `list_bookings` (documented in each tool's description) — the MCP server does
  no background polling, matching the portal's HTMX-polling model.
- **Secrets.** `create_booking`'s response carries one-time credentials (`password`/`ssh_key`) for
  static VMs exactly as the portal returns them; the server neither logs nor caches them.
  `list_bookings` never contains secrets (portal already strips them).
- **Missing/invalid token** → the portal's `401` surfaces as a tool error telling the user to set
  their portal API key in the MCP client config.
- **Timeouts/connection errors** to the portal → a clear tool error (`portal unreachable at
  <PORTAL_BASE_URL>`), not a crash.

## Implementation sketch

- `mcp_server/__main__.py` — builds the `FastMCP` server, runs it with the `streamable-http`
  transport on `MCP_HOST:MCP_PORT`.
- `mcp_server/server.py` — tool definitions (the table above). Each tool resolves the inbound
  `Authorization` header from the request context and delegates to the HTTP client.
- `mcp_server/portal_client.py` — a thin `httpx.AsyncClient` wrapper: takes a bearer token + base
  URL, one method per portal endpoint, raises a uniform `PortalError(status, detail)` on non-2xx.
- `mcp_server/config.py` — `PORTAL_BASE_URL`, `MCP_HOST`, `MCP_PORT` via pydantic-settings (matches
  the portal's `app/config.py` style).
- Deps: add `mcp` (the official Python SDK) and `httpx` to `requirements.txt` (`httpx` is already a
  dev dep; promote it). No DB, no Celery.
- `docker-compose`: an optional `mcp` service running `python -m mcp_server`, `PORTAL_BASE_URL`
  pointed at the `app` service, port `8765` exposed. (Added but documented as opt-in.)

## Testing

- `portal_client` against a mocked httpx transport: each method hits the right URL/verb/body and maps
  2xx→data, non-2xx→`PortalError`.
- Each tool: forwards the inbound bearer token, shapes args into the right portal call, and turns a
  `PortalError` into an MCP tool error carrying status + detail.
- Token forwarding: a request with no `Authorization` yields the "set your API key" error and makes
  no portal call.
- An end-to-end smoke test driving the in-process MCP server with the SDK client over the
  streamable-http transport against a stubbed portal, exercising `create_booking` → `list_bookings`.

## Docs

- `docs/api-reference.md` — short pointer that the same operations are available via MCP, listing the
  tool names.
- `docs/admin-guide.md` — how to run the MCP service (compose + standalone), the `PORTAL_BASE_URL`/
  host/port env vars, the TLS/network posture note, and **client config snippets for opencode and
  qwen-cli** (endpoint URL + `Authorization: Bearer dp_<key>` header).
