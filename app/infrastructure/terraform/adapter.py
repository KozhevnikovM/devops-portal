from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class TerraformAdapter(Protocol):
    async def apply(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Provision resources. Returns dict with at least {"ip": str}."""
        ...

    async def destroy(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> None:
        """Tear down resources for the given workspace."""
        ...
