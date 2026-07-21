"""Unit tests: ResourceFootprint, VMDetails, NamespaceDetails, StaticVMDetails (F-9).

Verifies VO construction, method correctness, and that _to_entity() populates
booking.details and booking.footprint for all three resource types.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.resource_details import (
    NamespaceDetails, ResourceFootprint, StaticVMDetails, VMDetails,
)
from app.domain.enums import BookingStatus, DriveType, ResourceType
from app.infrastructure.repositories.booking_repo import _to_entity
from app.infrastructure.database.models import BookingModel


def _base_model(**overrides) -> BookingModel:
    """Minimal BookingModel for _to_entity(); uses SimpleNamespace for speed."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid4(),
        user_id="u",
        status=BookingStatus.READY.value,
        resource_type=ResourceType.VM.value,
        ttl_minutes=60,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        cpus=4,
        memory_mb=8192,
        disk_mb=51200,
        drive_type=DriveType.HDD.value,
        status_message=None,
        startup_script=None,
        config_roles=[],
        extra_vars={},
        config_failed=False,
        environment_id=None,
        environment_label=None,
        label=None,
        created_by=None,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip="10.0.0.1",
        vm_password="pw",
        namespace_id=None,
        static_vm_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── ResourceFootprint ─────────────────────────────────────────────────────────

def test_footprint_memory_gb_ceiling():
    fp = ResourceFootprint(cpus=2, memory_mb=3000, disk_mb=0, drive_type="HDD")
    assert fp.memory_gb() == 3   # ceil(3000/1024) = 3

def test_footprint_memory_gb_exact():
    fp = ResourceFootprint(cpus=2, memory_mb=2048, disk_mb=0, drive_type="HDD")
    assert fp.memory_gb() == 2   # exact: ceil(2048/1024) = 2

def test_footprint_disk_gb_ceiling():
    fp = ResourceFootprint(cpus=0, memory_mb=0, disk_mb=1025, drive_type="SSD")
    assert fp.disk_gb() == 2     # ceil(1025/1024) = 2

def test_footprint_mb_to_gb_static():
    assert ResourceFootprint.mb_to_gb(0) == 0
    assert ResourceFootprint.mb_to_gb(1024) == 1
    assert ResourceFootprint.mb_to_gb(1025) == 2

def test_footprint_is_frozen():
    fp = ResourceFootprint(cpus=2, memory_mb=4096, disk_mb=0, drive_type="HDD")
    with pytest.raises((AttributeError, TypeError)):
        fp.cpus = 99  # type: ignore[misc]


# ── VMDetails ─────────────────────────────────────────────────────────────────

def test_vm_details_frozen():
    d = VMDetails(
        image_id=uuid4(), image_name="img", hw_config_id=uuid4(), hw_config_name="hw",
        vm_ip="1.2.3.4", vm_password="pw", startup_script=None, config_failed=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        d.vm_ip = "0.0.0.0"  # type: ignore[misc]

def test_vm_details_config_roles_is_tuple():
    d = VMDetails(
        image_id=None, image_name=None, hw_config_id=None, hw_config_name=None,
        vm_ip=None, vm_password=None, startup_script=None, config_failed=False,
        config_roles=({"name": "nginx"},),
    )
    assert isinstance(d.config_roles, tuple)


# ── _to_entity: VM ────────────────────────────────────────────────────────────

def test_to_entity_vm_populates_details():
    m = _base_model()
    b = _to_entity(m)
    assert isinstance(b.details, VMDetails)
    assert b.details.image_name == "Ubuntu 22.04"
    assert b.details.vm_ip == "10.0.0.1"
    assert b.details.vm_password == "pw"
    assert isinstance(b.details.config_roles, tuple)

def test_to_entity_vm_populates_footprint():
    m = _base_model()
    b = _to_entity(m)
    assert isinstance(b.footprint, ResourceFootprint)
    assert b.footprint.cpus == 4
    assert b.footprint.memory_mb == 8192
    assert b.footprint.memory_gb() == 8


# ── _to_entity: NAMESPACE ─────────────────────────────────────────────────────

def test_to_entity_namespace_populates_details():
    ns_id = uuid4()
    m = _base_model(
        resource_type=ResourceType.NAMESPACE.value,
        namespace_id=ns_id,
        image_id=None, image_name=None, hw_config_id=None, hw_config_name=None,
        vm_ip=None, vm_password=None, startup_script=None,
        cpus=0, memory_mb=0, disk_mb=0,
    )
    ns = SimpleNamespace(name="ns-1", cluster_name="cluster-a", api_url="https://api.cluster-a")
    b = _to_entity(m, namespace=ns)
    assert isinstance(b.details, NamespaceDetails)
    assert b.details.namespace_id == ns_id
    assert b.details.namespace_name == "ns-1"
    assert b.details.cluster_name == "cluster-a"
    assert b.details.api_url == "https://api.cluster-a"


# ── _to_entity: STATIC_VM ────────────────────────────────────────────────────

def test_to_entity_static_vm_populates_details():
    svm_id = uuid4()
    m = _base_model(
        resource_type=ResourceType.STATIC_VM.value,
        static_vm_id=svm_id,
        image_id=None, image_name=None, hw_config_id=None, hw_config_name=None,
        vm_ip=None, vm_password=None, startup_script=None,
        cpus=2, memory_mb=4096, disk_mb=20480,
    )
    svm = SimpleNamespace(name="svm-1", host="192.168.1.1", username="admin",
                          password="secret", ssh_key=None)
    b = _to_entity(m, static_vm=svm)
    assert isinstance(b.details, StaticVMDetails)
    assert b.details.static_vm_id == svm_id
    assert b.details.static_vm_host == "192.168.1.1"
    assert b.details.static_vm_password == "secret"
    assert b.details.static_vm_ssh_key is None
