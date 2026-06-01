import asyncio
import random
from typing import Callable


class StubTerraformAdapter:
    """Simulates Terraform provisioning without touching real infrastructure."""

    async def apply(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        if on_progress:
            on_progress("Provisioning (stub mode)…")
        await asyncio.sleep(5)
        ip = f"192.168.100.{random.randint(10, 254)}"
        return {"ip": ip}

    async def destroy(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        if on_progress:
            on_progress("Destroying (stub mode)…")
        await asyncio.sleep(2)
