from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.enums import BookingStatus, DriveType, ResourceType


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
