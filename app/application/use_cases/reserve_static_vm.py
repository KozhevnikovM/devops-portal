from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import StaticVMUnavailableError
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository


class ReserveStaticVMUseCase:
    """Reserve a static VM from the pool — synchronous, no provisioning.

    Two modes: reserve a *specific* VM by id, or auto-assign *any* free one.
    """

    def __init__(self, repo: BookingRepository, static_vm_repo: StaticVMRepository) -> None:
        self._repo = repo
        self._static_vm_repo = static_vm_repo

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
        static_vm_id: UUID | None = None,
    ) -> Booking:
        uid = user_id or settings.DEV_USER_ID

        now = datetime.now(timezone.utc)

        if static_vm_id is not None:
            # Pick-specific — lock the chosen row FOR UPDATE, reject if gone/inactive/taken.
            vm = await self._static_vm_repo.lock_for_allocation(session, static_vm_id)
            if vm is None or not vm.is_active:
                raise StaticVMUnavailableError("Static VM is not available")
            if await self._static_vm_repo.is_held(session, static_vm_id):
                raise StaticVMUnavailableError(f"Static VM '{vm.name}' is already booked")
        else:
            # Any-available — FOR UPDATE SKIP LOCKED so concurrent bookers take different VMs.
            vm = await self._static_vm_repo.lock_next_available(session)
            if vm is None:
                # Pool exhausted — enqueue (FIFO). Promoted to READY when one frees.
                return await self._enqueue(session, uid, ttl_minutes, now)

        if ttl_minutes == 0:
            expires_at = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        else:
            expires_at = now + timedelta(minutes=ttl_minutes)

        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.READY,  # nothing to provision — ready immediately
            resource_type=ResourceType.STATIC_VM,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            created_at=now,
            static_vm_id=vm.id,
        )
        created = await self._repo.create(session, booking)  # commit releases the row lock

        # The create() round-trip doesn't join the static VM; attach display fields directly.
        created.static_vm_name = vm.name
        created.static_vm_host = vm.host
        created.static_vm_username = vm.username
        created.static_vm_password = vm.password
        created.static_vm_ssh_key = vm.ssh_key
        return created

    async def _enqueue(self, session, uid: str, ttl_minutes: int, now: datetime) -> Booking:
        """No free static VM — create a QUEUED booking (no resource yet, TTL starts on promotion)."""
        booking = Booking(
            id=uuid4(),
            user_id=uid,
            status=BookingStatus.QUEUED,
            resource_type=ResourceType.STATIC_VM,
            ttl_minutes=ttl_minutes,
            expires_at=now,  # placeholder until promotion; enforce_ttl ignores QUEUED
            created_at=now,
        )
        return await self._repo.create(session, booking)
