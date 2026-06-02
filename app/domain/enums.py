from enum import Enum


class BookingStatus(str, Enum):
    PENDING      = "PENDING"
    PROVISIONING = "PROVISIONING"
    RETRY        = "RETRY"
    READY        = "READY"
    FAILED       = "FAILED"
    RELEASING    = "RELEASING"
    RELEASED     = "RELEASED"


class ResourceType(str, Enum):
    VM        = "VM"
    NAMESPACE = "NAMESPACE"
