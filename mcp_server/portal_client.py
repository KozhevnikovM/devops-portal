"""Thin async HTTP client over the portal's ``/api/*`` JSON endpoints.

One method per portal operation the MCP server exposes. Every method forwards the caller's bearer
token, maps a 2xx response to parsed JSON (or ``None`` for an empty 204), and raises a uniform
:class:`PortalError` carrying the status code and the portal's ``detail`` message for any non-2xx.
The client adds no business logic — validation, quota, and permissions are the portal's job.
"""
from __future__ import annotations

from typing import Any

import httpx


class PortalError(Exception):
    """A non-2xx response from the portal (or an unreachable portal).

    ``status`` is the HTTP status code, or ``None`` when the portal could not be reached at all.
    ``detail`` is the portal's ``detail`` field when present, else a human-readable fallback.
    """

    def __init__(self, status: int | None, detail: str) -> None:
        self.status = status
        self.detail = detail
        prefix = f"{status} " if status is not None else ""
        super().__init__(f"{prefix}{detail}")


def _detail(response: httpx.Response) -> str:
    """Best-effort extraction of the portal's error message."""
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        return detail if isinstance(detail, str) else str(detail)
    return str(body)


class PortalClient:
    """Per-request client bound to one caller's bearer token.

    A fresh instance is created for each tool invocation (cheap — it just holds config and a token).
    Pass ``client`` to inject a pre-built ``httpx.AsyncClient`` (used in tests); otherwise one is
    created per call.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise PortalError(None, f"portal unreachable at {self._base_url}: {exc}") from exc

        if response.status_code >= 400:
            raise PortalError(response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ── Discovery (read-only) ────────────────────────────────────────────────────
    async def list_vm_images(self) -> Any:
        return await self._request("GET", "/api/images")

    async def list_hardware_configs(self) -> Any:
        return await self._request("GET", "/api/hardware")

    async def list_static_vms(self) -> Any:
        return await self._request("GET", "/api/static-vms")

    async def list_roles(self) -> Any:
        return await self._request("GET", "/api/roles")

    async def list_blueprints(self) -> Any:
        return await self._request("GET", "/api/environment-blueprints")

    # ── Bookings ─────────────────────────────────────────────────────────────────
    async def list_bookings(self) -> Any:
        return await self._request("GET", "/api/bookings")

    async def create_booking(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/bookings", json=body)

    async def extend_booking(self, booking_id: str, extend_minutes: int) -> Any:
        return await self._request(
            "PUT", f"/api/bookings/{booking_id}/extend", json={"extend_minutes": extend_minutes}
        )

    async def release_booking(self, booking_id: str) -> Any:
        return await self._request("DELETE", f"/api/bookings/{booking_id}")

    async def get_booking_audit(self, booking_id: str) -> Any:
        return await self._request("GET", f"/api/bookings/{booking_id}/audit")

    # ── Environments ─────────────────────────────────────────────────────────────
    async def list_environments(self) -> Any:
        return await self._request("GET", "/api/environments")

    async def get_environment(self, environment_id: str) -> Any:
        return await self._request("GET", f"/api/environments/{environment_id}")

    async def create_environment(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/environments", json=body)

    async def release_environment(self, environment_id: str) -> Any:
        return await self._request("DELETE", f"/api/environments/{environment_id}")

    async def find_environment_by_namespace(
        self, namespace_name: str, cluster: str | None = None
    ) -> Any:
        params = {"cluster": cluster} if cluster else None
        return await self._request(
            "GET", f"/api/environments/by-namespace/{namespace_name}", params=params
        )
