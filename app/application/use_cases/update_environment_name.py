from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import EnvironmentRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.entities import Environment, User
from app.domain.exceptions import BookingPermissionError, EnvironmentError

_MAX_NAME_LENGTH = 128


class UpdateEnvironmentNameUseCase:
    """Rename an environment. Unlike a booking's optional label, an environment's name is
    required (NOT NULL), so a blank name is rejected."""

    def __init__(self, repo: EnvironmentRepositoryPort) -> None:
        self._repo = repo

    async def execute(
        self, session: AsyncSession, environment_id: UUID, name: str, current_user: User,
    ) -> Environment:
        env = await self._repo.get(session, environment_id)
        if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
            raise BookingPermissionError("not allowed to edit this environment")
        name = name.strip()
        if not name:
            raise EnvironmentError("name cannot be blank")
        if len(name) > _MAX_NAME_LENGTH:
            raise EnvironmentError(f"name must be at most {_MAX_NAME_LENGTH} characters")
        await self._repo.update_name(session, environment_id, name)
        return await self._repo.get(session, environment_id)
