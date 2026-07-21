"""Value objects encapsulating per-resource-type details and resource footprint (#F-9).

These are pure-Python, frozen dataclasses with zero framework imports.  They live alongside
``Booking`` in the domain layer and are populated by ``booking_repo._to_entity()`` from
the existing flat ORM columns — no migration required.

Flat fields on ``Booking`` are retained for backward compatibility and will be removed in v0.12.0
once all consumers have migrated to ``booking.details`` / ``booking.footprint`` accessors.
"""
import math
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class VMDetails:
    image_id: UUID | None
    image_name: str | None
    hw_config_id: UUID | None
    hw_config_name: str | None
    vm_ip: str | None
    vm_password: str | None
    startup_script: str | None
    config_failed: bool
    # Tuple is immutable; dict is shallow-mutable — excluded from __hash__ / __eq__.
    config_roles: tuple = field(default=(), hash=False, compare=False)
    extra_vars: dict   = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class NamespaceDetails:
    namespace_id: UUID | None
    namespace_name: str | None
    cluster_name: str | None
    api_url: str | None


@dataclass(frozen=True)
class StaticVMDetails:
    static_vm_id: UUID | None
    static_vm_name: str | None
    static_vm_host: str | None
    static_vm_username: str | None
    static_vm_password: str | None
    static_vm_ssh_key: str | None


@dataclass(frozen=True)
class ResourceFootprint:
    """Captures the resource consumption of one booking slot.

    ``mb_to_gb`` is the single authoritative implementation of the MB→GB rounding rule
    (``math.ceil``).  ``memory_gb`` and ``disk_gb`` delegate to it so callers that need
    ad-hoc conversion can use ``ResourceFootprint.mb_to_gb(x)`` without constructing an
    instance.
    """
    cpus: int
    memory_mb: int
    disk_mb: int
    drive_type: str

    @staticmethod
    def mb_to_gb(mb: int) -> int:
        return math.ceil(mb / 1024)

    def memory_gb(self) -> int:
        return ResourceFootprint.mb_to_gb(self.memory_mb)

    def disk_gb(self) -> int:
        return ResourceFootprint.mb_to_gb(self.disk_mb)
