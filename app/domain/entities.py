from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.booking_status import can_transition
from app.domain.enums import BookingStatus, DriveType, ResourceType
from app.domain.exceptions import IllegalStatusTransitionError


@dataclass
class User:
    id: UUID
    username: str
    password_hash: str
    role: str
    is_active: bool
    created_at: datetime
    timezone: str = "UTC"
    default_image_id: UUID | None = None
    default_hw_config_id: UUID | None = None


@dataclass
class APIKey:
    id: UUID
    key_hash: str
    user_id: UUID
    description: str | None
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None


@dataclass
class VMImage:
    id: UUID
    name: str
    vapp_template_id: str
    is_active: bool
    created_at: datetime


@dataclass
class HWConfig:
    id: UUID
    name: str
    cpus: int
    memory_mb: int
    disk_mb: int
    is_active: bool
    created_at: datetime
    drive_type: str = DriveType.HDD.value


@dataclass
class Namespace:
    id: UUID
    name: str
    cluster_name: str
    api_url: str | None
    is_active: bool
    created_at: datetime


@dataclass
class Role:
    """An Ansible role offered in the catalog, applied to a VM during configuration."""
    id: UUID
    name: str
    description: str | None
    ansible_role: str          # the role directory name under ansible/roles/
    default_vars: dict         # admin-set Ansible variables for this role
    is_active: bool
    created_at: datetime
    secret_vars: dict = field(default_factory=dict)  # per-key Fernet-encrypted blob; never log values

    def __repr__(self) -> str:
        keys = sorted(self.secret_vars.keys()) if self.secret_vars else []
        return (
            f"Role(id={self.id!r}, name={self.name!r}, ansible_role={self.ansible_role!r}, "
            f"is_active={self.is_active!r}, secret_vars_keys={keys!r})"
        )


@dataclass
class EnvironmentBlueprintItem:
    """One resource in a blueprint. `spec` carries per-type fields (catalog entries by name)."""
    id: UUID
    resource_type: str         # ResourceType value: VM | STATIC_VM | NAMESPACE
    position: int
    label: str | None
    spec: dict                 # VM: {image_name, hw_config_name, roles[], startup_script}; etc.


@dataclass
class EnvironmentBlueprint:
    """An admin-defined template bundling several resources into one orderable stack."""
    id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    items: list = field(default_factory=list)  # list[EnvironmentBlueprintItem]


@dataclass
class Environment:
    """An ordered stack: a parent owning N child bookings, with one shared TTL.

    `status` is not stored — it's derived from the child bookings' statuses.
    """
    id: UUID
    name: str
    blueprint_name: str | None
    user_id: str
    ttl_minutes: int
    expires_at: datetime
    created_at: datetime
    bookings: list = field(default_factory=list)  # list[Booking] — the children
    owner_username: str | None = None
    created_by: str | None = None  # acting dispatcher's id when ordered on behalf of the owner
    created_by_username: str | None = None  # resolved username of created_by (display only)


@dataclass
class StaticVM:
    id: UUID
    name: str
    host: str
    username: str
    password: str | None
    ssh_key: str | None
    cpus: int | None
    memory_mb: int | None
    is_active: bool
    created_at: datetime


@dataclass
class Booking:
    id: UUID
    user_id: str
    status: BookingStatus
    ttl_minutes: int
    expires_at: datetime
    created_at: datetime
    resource_type: ResourceType = ResourceType.VM
    image_id: UUID | None = None
    image_name: str | None = None
    hw_config_id: UUID | None = None
    hw_config_name: str | None = None
    vm_ip: str | None = None
    vm_password: str | None = None
    owner_username: str | None = None
    cpus: int = 0
    memory_mb: int = 0
    disk_mb: int = 0
    drive_type: str = DriveType.HDD.value
    status_message: str | None = None
    startup_script: str | None = None
    config_roles: list = field(default_factory=list)  # snapshot: [{name, ansible_role, vars}]
    extra_vars: dict = field(default_factory=dict)    # blueprint-level vars injected into ansible portal dict
    config_failed: bool = False
    environment_id: UUID | None = None  # parent Environment, when ordered as part of a stack
    environment_label: str | None = None  # blueprint item label (e.g. "web") within the stack
    label: str | None = None              # user-provided display name (e.g. "my perf test")
    created_by: str | None = None  # acting dispatcher's id when ordered on behalf of the owner
    created_by_username: str | None = None  # resolved username of created_by (display only)
    namespace_id: UUID | None = None
    namespace_name: str | None = None
    cluster_name: str | None = None
    api_url: str | None = None
    static_vm_id: UUID | None = None
    static_vm_name: str | None = None
    static_vm_host: str | None = None
    static_vm_username: str | None = None
    static_vm_password: str | None = None
    static_vm_ssh_key: str | None = None
    queue_position: int | None = None  # FIFO rank for QUEUED bookings (display only)

    def transition_to(self, new: BookingStatus) -> None:
        """Enforce the status-transition invariant and advance self.status.

        No-op when old == new (idempotent re-write). Raises IllegalStatusTransitionError
        for any disallowed move.
        """
        if self.status == new:
            return
        if not can_transition(self.status, new):
            raise IllegalStatusTransitionError(
                f"Cannot move booking {self.id} from {self.status.value} to {new.value}"
            )
        self.status = new


@dataclass
class VM:
    id: UUID
    booking_id: UUID
    workspace_id: str
    ip_address: str | None
    created_at: datetime


@dataclass
class BookingAuditEntry:
    id: UUID
    booking_id: UUID
    actor_id: str
    action: str
    old_status: str | None
    new_status: str | None
    metadata: dict | None
    created_at: datetime


@dataclass
class Quota:
    id: UUID
    user_id: UUID
    max_cpus: int
    max_memory_gb: int
    max_ssd_gb: int
    max_hdd_gb: int
    created_at: datetime
