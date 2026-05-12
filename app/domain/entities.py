from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import BookingStatus


@dataclass
class Booking:
    id: UUID
    user_id: str
    status: BookingStatus
    ttl_hours: int
    expires_at: datetime
    created_at: datetime
    vm_ip: str | None = None


@dataclass
class VM:
    id: UUID
    booking_id: UUID
    workspace_id: str
    ip_address: str | None
    created_at: datetime
