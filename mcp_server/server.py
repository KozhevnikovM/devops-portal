"""FastMCP server definition: one tool per portal self-service operation.

Each tool pulls the caller's portal API key from the inbound ``Authorization`` header (token
forwarding — the server holds no credentials), calls the matching :class:`PortalClient` method, and
returns the portal's JSON. A :class:`PortalError` becomes an MCP ``ToolError`` whose message carries
the portal's status code and detail, so the model can react (smaller config, release something, …).
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server.config import settings
from mcp_server.portal_client import PortalClient, PortalError

mcp = FastMCP(
    "devops-portal",
    host=settings.MCP_HOST,
    port=settings.MCP_PORT,
    instructions=(
        "Tools for the DevOps Portal: discover the catalog (VM images, hardware configs, static VMs, "
        "Ansible roles, environment blueprints) and manage bookings and environments (create, list, "
        "extend, release, audit). Order VMs by image_name + hw_config_name, namespaces by "
        "(namespace_name, cluster_name), static VMs by static_vm_name — discover valid names with the "
        "list_* tools. VM bookings provision asynchronously: create_booking returns PENDING/"
        "PROVISIONING (or QUEUED for an empty pool); poll list_bookings until READY/FAILED. Set the "
        "portal API key as an 'Authorization: Bearer dp_<key>' header on the MCP connection."
    ),
)


def _client(ctx: Context) -> PortalClient:
    """Build a PortalClient bound to the caller's forwarded API key.

    Reads the inbound ``Authorization: Bearer dp_<key>`` header from the HTTP request. Raises a
    ToolError (no portal call made) when it is absent or malformed.
    """
    request = getattr(ctx.request_context, "request", None)
    auth = request.headers.get("Authorization", "") if request is not None else ""
    if not auth.startswith("Bearer "):
        raise ToolError(
            "No portal API key. Configure your MCP client to send an "
            "'Authorization: Bearer dp_<your_api_key>' header."
        )
    token = auth[len("Bearer "):].strip()
    return PortalClient(
        settings.PORTAL_BASE_URL, token, timeout=settings.PORTAL_TIMEOUT
    )


async def _call(ctx: Context, coro_factory: Any) -> Any:
    """Run a PortalClient call, translating PortalError → ToolError."""
    client = _client(ctx)
    try:
        return await coro_factory(client)
    except PortalError as exc:
        raise ToolError(str(exc)) from exc


# ── Discovery (read-only) ────────────────────────────────────────────────────────
@mcp.tool()
async def list_vm_images(ctx: Context) -> Any:
    """List VM images in the catalog (name, id, active flag). Use a name with create_booking."""
    return await _call(ctx, lambda c: c.list_vm_images())


@mcp.tool()
async def list_hardware_configs(ctx: Context) -> Any:
    """List hardware configs (name, cpus, memory, disk, drive_type). Use a name with create_booking."""
    return await _call(ctx, lambda c: c.list_hardware_configs())


@mcp.tool()
async def list_static_vms(ctx: Context) -> Any:
    """List bookable static VMs (name, host, available flag). Credentials are not included here."""
    return await _call(ctx, lambda c: c.list_static_vms())


@mcp.tool()
async def list_roles(ctx: Context) -> Any:
    """List Ansible roles applicable to a VM booking via the `roles` field of create_booking."""
    return await _call(ctx, lambda c: c.list_roles())


@mcp.tool()
async def list_blueprints(ctx: Context) -> Any:
    """List environment blueprints (named resource bundles) orderable with create_environment."""
    return await _call(ctx, lambda c: c.list_blueprints())


# ── Bookings ─────────────────────────────────────────────────────────────────────
@mcp.tool()
async def list_bookings(ctx: Context) -> Any:
    """List your bookings (admins see all). Poll this to watch a VM booking reach READY/FAILED."""
    return await _call(ctx, lambda c: c.list_bookings())


@mcp.tool()
async def create_booking(
    ctx: Context,
    ttl_minutes: int,
    resource_type: str = "VM",
    image_name: str | None = None,
    hw_config_name: str | None = None,
    image_id: str | None = None,
    hw_config_id: str | None = None,
    namespace_name: str | None = None,
    cluster_name: str | None = None,
    namespace_id: str | None = None,
    static_vm_name: str | None = None,
    static_vm_id: str | None = None,
    startup_script: str | None = None,
    roles: list[str] | None = None,
    on_behalf_of: str | None = None,
) -> Any:
    """Create a booking. Returns the created booking JSON (VM: PENDING/PROVISIONING; namespace/static
    VM: READY with details; pooled "any available" on an empty pool: QUEUED).

    - resource_type: "VM" (default), "STATIC_VM", or "NAMESPACE".
    - ttl_minutes: duration; 0 = no expiry.
    - VM: give image + hardware by name (image_name/hw_config_name, preferred) or id. Optional
      startup_script (idempotent bash run over SSH) and roles (catalog names from list_roles).
    - NAMESPACE: omit ids for "any available", or give (namespace_name + cluster_name) or namespace_id.
    - STATIC_VM: omit for "any available" (queues if pool empty), or give static_vm_name/static_vm_id.
    - on_behalf_of: dispatcher/admin only — order for another user by username.
    """
    body: dict[str, Any] = {"resource_type": resource_type, "ttl_minutes": ttl_minutes}
    optional = {
        "image_name": image_name,
        "hw_config_name": hw_config_name,
        "image_id": image_id,
        "hw_config_id": hw_config_id,
        "namespace_name": namespace_name,
        "cluster_name": cluster_name,
        "namespace_id": namespace_id,
        "static_vm_name": static_vm_name,
        "static_vm_id": static_vm_id,
        "startup_script": startup_script,
        "roles": roles,
        "on_behalf_of": on_behalf_of,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    return await _call(ctx, lambda c: c.create_booking(body))


@mcp.tool()
async def extend_booking(ctx: Context, booking_id: str, extend_minutes: int) -> Any:
    """Add `extend_minutes` to a READY booking's TTL (owner only; permanent bookings cannot extend)."""
    return await _call(ctx, lambda c: c.extend_booking(booking_id, extend_minutes))


@mcp.tool()
async def release_booking(ctx: Context, booking_id: str) -> Any:
    """Release a booking (owner or admin). VMs tear down (RELEASING→RELEASED); pooled return to pool."""
    return await _call(ctx, lambda c: c.release_booking(booking_id))


@mcp.tool()
async def get_booking_audit(ctx: Context, booking_id: str) -> Any:
    """Get a booking's chronological audit trail (status transitions, actors, metadata)."""
    return await _call(ctx, lambda c: c.get_booking_audit(booking_id))


# ── Environments ─────────────────────────────────────────────────────────────────
@mcp.tool()
async def list_environments(ctx: Context) -> Any:
    """List your environments (admins see all), each with derived status and child summaries."""
    return await _call(ctx, lambda c: c.list_environments())


@mcp.tool()
async def get_environment(ctx: Context, environment_id: str) -> Any:
    """Fetch one environment by id (owner or admin), with its derived status and children."""
    return await _call(ctx, lambda c: c.get_environment(environment_id))


@mcp.tool()
async def create_environment(
    ctx: Context, blueprint_name: str, ttl_minutes: int, on_behalf_of: str | None = None
) -> Any:
    """Order an environment blueprint: one parent environment plus its child bookings under one TTL.

    - blueprint_name: a name from list_blueprints.
    - ttl_minutes: shared duration; 0 = no expiry.
    - on_behalf_of: dispatcher/admin only — order for another user by username.
    """
    body: dict[str, Any] = {"blueprint_name": blueprint_name, "ttl_minutes": ttl_minutes}
    if on_behalf_of is not None:
        body["on_behalf_of"] = on_behalf_of
    return await _call(ctx, lambda c: c.create_environment(body))


@mcp.tool()
async def release_environment(ctx: Context, environment_id: str) -> Any:
    """Release a whole environment — tears down all its children together (owner or admin)."""
    return await _call(ctx, lambda c: c.release_environment(environment_id))


@mcp.tool()
async def find_environment_by_namespace(
    ctx: Context, namespace_name: str, cluster: str | None = None
) -> Any:
    """Find the live environment whose namespace child is `namespace_name` (read-only lookup).

    Optional `cluster` disambiguates a namespace name reused across clusters. 409 if another user owns
    it (owner not disclosed), 404 if no active environment holds it.
    """
    return await _call(ctx, lambda c: c.find_environment_by_namespace(namespace_name, cluster))
