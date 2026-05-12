from typing import Protocol, runtime_checkable


@runtime_checkable
class TerraformAdapter(Protocol):
    async def apply(self, workspace_id: str, config: dict) -> dict:
        """Provision resources. Returns dict with at least {"ip": str}."""
        ...

    async def destroy(self, workspace_id: str) -> None:
        """Tear down resources for the given workspace."""
        ...
