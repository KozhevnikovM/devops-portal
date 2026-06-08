from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingPermissionError, EnvironmentError

_TERMINAL = {BookingStatus.RELEASED, BookingStatus.FAILED}


class EnvironmentNotFoundError(EnvironmentError):
    pass


class ReleaseEnvironmentUseCase:
    """Release a whole environment: force-release every non-terminal child booking.

    The stack is going away, so children are torn down regardless of state (provisioned VMs →
    teardown, pooled → back to the pool + promote, queued → cancelled). The environment row is kept;
    its derived status becomes RELEASED once the children settle.
    """

    def __init__(self, env_repo, release_booking_use_case) -> None:
        self._env_repo = env_repo
        self._release = release_booking_use_case

    async def execute(self, session: AsyncSession, environment_id: UUID, current_user: User) -> Environment:
        try:
            env = await self._env_repo.get(session, environment_id)
        except ValueError:
            raise EnvironmentNotFoundError(f"Environment {environment_id} not found")

        if env.user_id != str(current_user.id) and current_user.role != "admin":
            raise BookingPermissionError("Not the environment owner")

        for child in env.bookings:
            if child.status in _TERMINAL:
                continue  # idempotent — already gone
            await self._release.execute(session, child.id, current_user, force=True)

        return await self._env_repo.get(session, environment_id)
