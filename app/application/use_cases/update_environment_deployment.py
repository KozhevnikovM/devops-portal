"""Replace a complete deployment report inside the parent serialization boundary."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import EnvironmentRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.deployment import DeploymentReport
from app.domain.entities import Environment, User
from app.domain.exceptions import BookingPermissionError


class UpdateEnvironmentDeploymentUseCase:
    def __init__(self, repo: EnvironmentRepositoryPort) -> None:
        self._repo = repo

    async def execute(
        self,
        session: AsyncSession,
        environment_id: UUID,
        report: DeploymentReport,
        current_user: User,
    ) -> Environment:
        try:
            env = await self._repo.get_for_update(session, environment_id)
            if not can_manage(
                owner_id=env.user_id, created_by=env.created_by, user=current_user
            ):
                raise BookingPermissionError(
                    "not allowed to report this environment's deployment"
                )
            return await self._repo.update_deployment(session, environment_id, report)
        except Exception:
            await session.rollback()
            raise
