"""Tests for ordering a VM/static VM by catalog name + catalog discovery over the API (#201).

Names (image, hardware, static VM) are globally unique, so POST /api/bookings accepts them in
place of ids; GET /api/images, /api/hardware, /api/static-vms make names discoverable to any
authenticated user.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, StaticVM
from app.domain.enums import BookingStatus, ResourceType


def _vm_booking() -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu 22.04", hw_config_id=uuid4(), hw_config_name="medium",
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Order a VM by names ───────────────────────────────────────────────────────
def test_order_vm_by_names_resolves_to_ids(client):
    booking = _vm_booking()
    image_id, hw_id = uuid4(), uuid4()
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.api_bookings._create_use_case") as uc:
        img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=image_id))
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=hw_id))
        uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "Ubuntu 22.04", "hw_config_name": "medium",
        })

    assert resp.status_code == 201
    # The resolved ids were passed positionally to the create use case.
    args = uc.execute.call_args.args
    assert image_id in args and hw_id in args


def test_order_vm_unknown_image_name_returns_400(client):
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw:
        img.get_by_name = AsyncMock(return_value=None)
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "nope", "hw_config_name": "medium",
        })
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"]


def test_order_vm_missing_image_and_id_returns_400(client):
    # hw given by name (resolves), but image has neither id nor name -> 400.
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw:
        img.get_by_name = AsyncMock(return_value=None)
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240, "hw_config_name": "medium",
        })
    assert resp.status_code == 400


def test_order_vm_id_takes_precedence_over_name(client):
    booking = _vm_booking()
    explicit_image, explicit_hw = uuid4(), uuid4()
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.api_bookings._create_use_case") as uc:
        img.get_by_name = AsyncMock()
        hw.get_by_name = AsyncMock()
        uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_id": str(explicit_image), "image_name": "ignored",
            "hw_config_id": str(explicit_hw), "hw_config_name": "ignored",
        })

    assert resp.status_code == 201
    img.get_by_name.assert_not_called()
    hw.get_by_name.assert_not_called()
    args = uc.execute.call_args.args
    assert explicit_image in args and explicit_hw in args


# ── Order a static VM by name ─────────────────────────────────────────────────
def test_order_static_vm_by_name_resolves(client):
    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid4(), user_id="u", status=BookingStatus.READY, resource_type=ResourceType.STATIC_VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        static_vm_name="build-agent-1",
    )
    svm_id = uuid4()
    with patch("app.presentation.routes.api_bookings._static_vm_repo") as svm, \
         patch("app.presentation.routes.api_bookings._reserve_static_vm_use_case") as uc:
        svm.get_by_name = AsyncMock(return_value=SimpleNamespace(id=svm_id))
        uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "STATIC_VM", "ttl_minutes": 240, "static_vm_name": "build-agent-1",
        })

    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["static_vm_id"] == svm_id


def test_order_static_vm_unknown_name_returns_400(client):
    with patch("app.presentation.routes.api_bookings._static_vm_repo") as svm:
        svm.get_by_name = AsyncMock(return_value=None)
        resp = client.post("/api/bookings", json={
            "resource_type": "STATIC_VM", "ttl_minutes": 240, "static_vm_name": "nope",
        })
    assert resp.status_code == 400


# ── Discovery: GET /api/static-vms ────────────────────────────────────────────
def _static_vm(name, vm_id) -> StaticVM:
    return StaticVM(
        id=vm_id, name=name, host="10.0.0.12", username="ubuntu",
        password="s3cret", ssh_key="ssh-ed25519 AAAA", cpus=4, memory_mb=8192,
        is_active=True, created_at=datetime.now(timezone.utc),
    )


def test_list_static_vms_omits_secrets_and_flags_availability(client):
    free_id, held_id = uuid4(), uuid4()
    vms = [_static_vm("free-agent", free_id), _static_vm("busy-agent", held_id)]
    with patch("app.presentation.routes.api._static_vm_repo") as svm:
        svm.list_active = AsyncMock(return_value=vms)
        svm.held_by = AsyncMock(return_value={held_id: "alice"})
        resp = client.get("/api/static-vms")

    assert resp.status_code == 200
    body = resp.json()
    assert "s3cret" not in resp.text and "ssh-ed25519" not in resp.text
    by_name = {v["name"]: v for v in body}
    assert by_name["free-agent"]["available"] is True
    assert by_name["busy-agent"]["available"] is False
    assert set(by_name["free-agent"].keys()) == {
        "id", "name", "host", "cpus", "memory_mb", "is_active", "available",
    }


# ── Relaxed read auth: non-admin can list images/hardware ─────────────────────
def test_non_admin_can_list_images(client):
    with patch("app.presentation.routes.api._image_repo") as img:
        img.list_all = AsyncMock(return_value=[])
        resp = client.get("/api/images")
    assert resp.status_code == 200


def test_non_admin_can_list_hardware(client):
    with patch("app.presentation.routes.api._hw_config_repo") as hw:
        hw.list_all = AsyncMock(return_value=[])
        resp = client.get("/api/hardware")
    assert resp.status_code == 200
