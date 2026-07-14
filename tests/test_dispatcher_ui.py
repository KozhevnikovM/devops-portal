"""Tests for the dispatcher UI & role validation (v0.9.0 P3, #231)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType


def _user(role="user", username="someone", uid=None):
    return User(id=uid or uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _booking(owner_id, created_by=None, created_by_username=None, status=BookingStatus.READY):
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id=owner_id, status=status, resource_type=ResourceType.VM,
                   ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                   image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
                   vm_ip="10.0.0.1", owner_username="john", created_by=created_by,
                   created_by_username=created_by_username)


def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user, require_admin
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app), app


# ── Server-side role validation ──────────────────────────────────────────────────
def test_api_create_user_accepts_dispatcher():
    admin = _user(role="admin")
    cl, app = _client(admin)
    try:
        with patch("app.presentation.routes.auth._user_repo") as repo:
            created = _user(role="dispatcher", username="ci-bot")
            repo.create = AsyncMock(return_value=created)
            resp = cl.post("/api/users", json={"username": "ci-bot", "password": "password123", "role": "dispatcher"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    assert resp.json()["role"] == "dispatcher"


def test_api_create_user_rejects_unknown_role():
    admin = _user(role="admin")
    cl, app = _client(admin)
    try:
        with patch("app.presentation.routes.auth._user_repo") as repo:
            repo.create = AsyncMock()
            resp = cl.post("/api/users", json={"username": "x", "password": "x", "role": "superuser"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    repo.create.assert_not_called()


def test_admin_ui_create_user_rejects_unknown_role():
    admin = _user(role="admin")
    cl, app = _client(admin)
    try:
        with patch("app.presentation.routes.auth._user_repo") as repo:
            repo.create = AsyncMock()
            resp = cl.post("/admin/users", data={"username": "x", "password": "x", "role": "root"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200  # inline HX error, not a hard failure
    assert "Invalid role" in resp.text
    repo.create.assert_not_called()


# ── Role badge in the user table ─────────────────────────────────────────────────
def test_user_table_renders_dispatcher_badge():
    admin = _user(role="admin")
    disp = _user(role="dispatcher", username="ci-bot")
    cl, app = _client(admin)
    try:
        with patch("app.presentation.routes.auth._user_repo") as repo, \
             patch("app.presentation.routes.auth._quota_repo") as quota:
            repo.list_all = AsyncMock(return_value=[disp])
            quota.get_limits = AsyncMock(return_value={})
            resp = cl.get("/admin/users/table")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert ">dispatcher<" in resp.text


# ── "via dispatcher" marker + can_manage on the booking row ──────────────────────
def test_booking_row_shows_via_marker_for_dispatched():
    viewer = _user(role="admin")  # admin can view any row
    booking = _booking("owner-id", created_by=str(uuid4()), created_by_username="ci-bot")
    cl, app = _client(viewer)
    try:
        with patch("app.presentation.routes.bookings._repo") as repo:
            repo.get = AsyncMock(return_value=booking)
            repo.queue_position = AsyncMock(return_value=None)
            resp = cl.get(f"/bookings/{booking.id}/row")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "via ci-bot" in resp.text


def test_self_order_row_has_no_via_marker():
    owner = _user(role="user", username="john")
    booking = _booking(str(owner.id), created_by=None, created_by_username=None)
    booking.owner_username = "john"
    cl, app = _client(owner)
    try:
        with patch("app.presentation.routes.bookings._repo") as repo:
            repo.get = AsyncMock(return_value=booking)
            repo.queue_position = AsyncMock(return_value=None)
            resp = cl.get(f"/bookings/{booking.id}/row")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "via " not in resp.text


def test_creating_dispatcher_sees_release_button_not_creds():
    disp = _user(role="dispatcher", username="ci-bot")
    # Dispatched VM the dispatcher created; owner is someone else. Password present in entity.
    booking = _booking("owner-id", created_by=str(disp.id), created_by_username="ci-bot")
    booking.vm_password = "secret-pw"
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.bookings._repo") as repo:
            repo.get = AsyncMock(return_value=booking)
            repo.queue_position = AsyncMock(return_value=None)
            resp = cl.get(f"/bookings/{booking.id}/row")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "Release" in resp.text                # manage control is offered
    assert "secret-pw" not in resp.text          # but credentials are not re-vended


# ── Repo join populates created_by_username ──────────────────────────────────────
@pytest.mark.asyncio
async def test_get_resolves_created_by_username():
    """booking_repo.get joins the creator alias and maps it to created_by_username."""
    from app.infrastructure.repositories.booking_repo import _to_entity
    from app.infrastructure.database.models import BookingModel
    m = MagicMock(spec=BookingModel)
    # Minimal attrs _to_entity reads:
    for attr, val in {
        "id": uuid4(), "user_id": "owner", "status": "READY", "resource_type": "VM",
        "ttl_minutes": 240, "expires_at": datetime.now(timezone.utc), "created_at": datetime.now(timezone.utc),
        "image_id": uuid4(), "image_name": "U", "hw_config_id": uuid4(), "hw_config_name": "m",
        "vm_ip": None, "vm_password": None, "cpus": 0, "memory_mb": 0, "disk_mb": 0, "drive_type": "HDD",
        "status_message": None, "startup_script": None, "config_roles": None, "config_failed": False,
        "environment_id": None, "environment_label": None, "created_by": "disp-id",
        "namespace_id": None, "static_vm_id": None,
    }.items():
        setattr(m, attr, val)
    entity = _to_entity(m, owner_username="john", created_by_username="ci-bot")
    assert entity.created_by == "disp-id"
    assert entity.created_by_username == "ci-bot"
