import asyncio
import random


class StubTerraformAdapter:
    """Simulates Terraform provisioning without touching real infrastructure."""

    async def apply(self, workspace_id: str, config: dict, api_token: str | None = None) -> dict:
        await asyncio.sleep(5)
        ip = f"192.168.100.{random.randint(10, 254)}"
        return {"ip": ip}

    async def destroy(self, workspace_id: str) -> None:
        await asyncio.sleep(2)
