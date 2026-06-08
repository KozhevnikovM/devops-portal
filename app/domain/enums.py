from enum import Enum


class BookingStatus(str, Enum):
    QUEUED       = "QUEUED"   # pooled booking waiting for a free resource (FIFO)
    PENDING      = "PENDING"
    PROVISIONING = "PROVISIONING"
    CONFIGURING  = "CONFIGURING"  # VM provisioned; running post-create config (bash/Ansible)
    RETRY        = "RETRY"
    READY        = "READY"
    FAILED       = "FAILED"
    RELEASING    = "RELEASING"
    RELEASED     = "RELEASED"


class ResourceType(str, Enum):
    VM        = "VM"
    STATIC_VM = "STATIC_VM"
    NAMESPACE = "NAMESPACE"


class DriveType(str, Enum):
    SSD = "SSD"
    HDD = "HDD"
