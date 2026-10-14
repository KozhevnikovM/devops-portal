from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import EnvironmentRepositoryPort
from app.application.use_cases._permissions import can_manage
from app.domain.entities import Environment, User
from app.domain.environment_status import derive_environment_status
from app.domain.exceptions import BookingPermissionError, EnvironmentError
from app.domain.lease import Lease, environment_extend_blocker


class ExtendEnvironmentUseCase:
    """Extend an environment's shared lease — the parent and every child to one new deadline (#532).

    The environment counterpart of ``ExtendBookingUseCase``: authorization first, then the
    eligibility and increment checks as a fast early answer. The repository rechecks eligibility
    under the environment and child row locks, which is what makes it safe against a concurrent
    release, TTL sweep or second extension.
    """

    def __init__(self, repo: EnvironmentRepositoryPort) -> None:
        self._repo = repo

    async def execute(
        self,
        session: AsyncSession,
        environment_id: UUID,
        extend_minutes: int,
        current_user: User,
    ) -> Environment:
        env = await self._repo.get(
            session, environment_id
        )  # raises EnvironmentNotFoundError
        # Authorization first — anyone without management rights gets 403 regardless of the
        # environment's status/TTL, so its lifecycle state never leaks.
        if not can_manage(
            owner_id=env.user_id, created_by=env.created_by, user=current_user
        ):
            raise BookingPermissionError("not allowed to extend this environment")
        lease = Lease(env.ttl_minutes, env.expires_at)
        blocker = environment_extend_blocker(
            derive_environment_status(b.status for b in env.bookings),
            lease,
        )
        if blocker is not None:
            raise EnvironmentError(blocker)
        lease.extended_within_term_by(extend_minutes)  # raises InvalidExtensionError
        await self._repo.extend_lease(
            session, environment_id, extend_minutes, actor_id=str(current_user.id)
        )
        return await self._repo.get(session, environment_id)
