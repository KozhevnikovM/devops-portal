"""Configuration for the MCP server.

Mirrors the portal's ``app/config.py`` style (pydantic-settings, ``.env`` support). The server needs
only where to find the portal and where to bind its own HTTP listener — it stores **no** credentials
(those are forwarded per request, see ``portal_client``).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Where the portal's JSON API lives. In docker-compose this points at the ``app`` service.
    PORTAL_BASE_URL: str = "http://localhost:8000"

    # Bind address for this MCP server's Streamable HTTP listener.
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8765

    # Timeout (seconds) for a single call to the portal before the tool reports it unreachable.
    PORTAL_TIMEOUT: float = 30.0


settings = MCPSettings()
