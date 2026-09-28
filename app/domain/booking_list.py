"""Read model for bulk booking lists (#477).

`BookingListItem` carries only what the bookings table and the JSON list summary read. Its
attribute names match `Booking`'s, so the shared row partial renders either one. Detail-only
payloads (provisioning log, startup script, extra-vars, role vars) are never loaded for a list:
the log is reduced to `has_provisioning_log` and the roles to `config_role_names`.
"""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import BookingStatus, ResourceType


@dataclass(slots=True)
class BookingListItem:
    id: UUID
    user_id: str
    status: BookingStatus
    resource_type: ResourceType
    ttl_minutes: int
    expires_at: datetime
    created_at: datetime
    label: str | None
    status_message: str | None
    config_failed: bool
    environment_id: UUID | None
    owner_username: str | None
    created_by: str | None
    created_by_username: str | None
    image_id: UUID | None
    image_name: str | None
    hw_config_id: UUID | None
    hw_config_name: str | None
    vm_ip: str | None
    vm_password: str | None
    namespace_name: str | None
    cluster_name: str | None
    api_url: str | None
    static_vm_name: str | None
    static_vm_host: str | None
    static_vm_username: str | None
    static_vm_password: str | None
    static_vm_ssh_key: str | None
    has_provisioning_log: bool
    config_role_names: tuple[str | None, ...]
    # Display only, set after the read (FIFO rank of a QUEUED booking) — the one mutable field.
    queue_position: int | None = None
