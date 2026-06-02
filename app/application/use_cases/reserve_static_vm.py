from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import StaticVMUnavailableError
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository


class ReserveStaticVMUseCase:
    """Reserve the next free static VM from the pool — synchronous, no provisioning."""

    def __init__(self, repo: BookingRepository, static_vm_repo: StaticVMRepository) -> None:
        self._repo = repo
        self._static_vm_repo = static_vm_repo

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
    ) -> Booking:
        uid = user_id or settings.DEV_USER_ID

        # FOR UPDATE SKIP LOCKED — concurrent reservations each take a different free VM.
        vm = await self._static_vm_repo.lock_next_available(session)
        if vm is None:
            raise StaticVMUnavailableError("No static VMs available")

        now = datetime.now(timezone.utc)
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
