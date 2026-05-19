from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.enums import BookingStatus


@dataclass
class User:
    id: UUID
    username: str
    password_hash: str
    role: str
    is_active: bool
    created_at: datetime
    timezone: str = "UTC"


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
    hdd_mb: int
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
    image_id: UUID
    image_name: str
    hw_config_id: UUID
    hw_config_name: str
    vm_ip: str | None = None
    vm_password: str | None = None
    owner_username: str | None = None
    cpus: int = 0
    memory_mb: int = 0
    hdd_mb: int = 0


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
    max_hdd_gb: int
    created_at: datetime
