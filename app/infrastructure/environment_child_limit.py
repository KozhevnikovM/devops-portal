"""The effective environment child limit, C_eff (#497 D3a).

Blueprint saves and orders enforce ENVIRONMENT_MAX_CHILDREN, so an environment created by this
version never exceeds it. Environments that are still live may exceed it anyway: they were ordered
before the limit existed, or by the previous app version still serving during a rolling or
blue-green deploy (it doesn't enforce the limit). C_eff = max(configured limit, the largest live
environment) covers them all, so page reconciliation's per-environment child read stays bounded
by a known value and still sees every child.

It is computed at startup, recomputed every ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS, and recomputed
promptly when a reconciliation request meets an environment over it — so an environment the old
slot created after this process started is covered within one refresh, without a restart. A
request reads `value` once; the refresh never changes the bound of a request in flight.
"""
import asyncio
import logging

from app.infrastructure.database.session import AsyncSessionLocal
from app.infrastructure.repositories.environment_repo import EnvironmentRepository

logger = logging.getLogger(__name__)


class EnvironmentChildLimit:
    def __init__(self, configured: int, repo: EnvironmentRepository | None = None,
                 session_factory=AsyncSessionLocal) -> None:
        self.configured = configured
        self.value = configured
        self._repo = repo or EnvironmentRepository()
        self._session_factory = session_factory
        self._wake = asyncio.Event()

    async def refresh(self) -> int:
        """Recompute C_eff; on a database error keep the current value and log it."""
        try:
            async with self._session_factory() as session:
                largest, over = await self._repo.live_children_over(session, self.configured)
        except Exception:
            logger.exception("could not recompute the effective environment child limit; keeping %d",
                             self.value)
            return self.value
        value = max(self.configured, largest)
        if value > self.configured and value != self.value:
            logger.warning(
                "%d live environment(s) exceed ENVIRONMENT_MAX_CHILDREN=%d (largest: %d children); "
                "using %d as the effective child limit until they are released",
                over, self.configured, largest, value,
            )
        self.value = value
        return value

    def request_refresh(self) -> None:
        """Ask the refresh loop to recompute now (a reconciliation met an environment over C_eff)."""
        self._wake.set()

    async def run(self, interval_seconds: float) -> None:
        """Refresh every `interval_seconds`, or sooner when asked. Runs until cancelled."""
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self.refresh()
