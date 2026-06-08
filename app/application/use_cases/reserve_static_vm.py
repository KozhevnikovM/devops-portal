from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Booking
from app.domain.enums import ResourceType
from app.domain.exceptions import StaticVMUnavailableError
from app.application.use_cases.reserve_pooled_resource import (
    PooledResourceConfig, ReservePooledResourceUseCase,
)
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository


def _attach_static_vm(booking: Booking, vm) -> None:
    booking.static_vm_name = vm.name
    booking.static_vm_host = vm.host
    booking.static_vm_username = vm.username
    booking.static_vm_password = vm.password
    booking.static_vm_ssh_key = vm.ssh_key


_STATIC_VM_CONFIG = PooledResourceConfig(
    resource_type=ResourceType.STATIC_VM,
    unavailable_exc=StaticVMUnavailableError,
    label="Static VM",
    fk_field="static_vm_id",
    attach_display=_attach_static_vm,
)


class ReserveStaticVMUseCase(ReservePooledResourceUseCase):
    """Reserve a static VM from the pool (specific or any-available, else enqueue)."""

    def __init__(self, repo: BookingRepository, static_vm_repo: StaticVMRepository) -> None:
        super().__init__(repo, static_vm_repo, _STATIC_VM_CONFIG)

    async def execute(
        self,
        session: AsyncSession,
        ttl_minutes: int,
        user_id: str | None = None,
        static_vm_id: UUID | None = None,
        environment_id: UUID | None = None,
        environment_label: str | None = None,
    ) -> Booking:
        return await super().execute(
            session, ttl_minutes, user_id=user_id, resource_id=static_vm_id,
            environment_id=environment_id, environment_label=environment_label,
        )
