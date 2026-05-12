from enum import Enum


class BookingStatus(str, Enum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    FAILED = "FAILED"
