"""Tests for the dispatcher role + on-behalf ordering (v0.9.0 P1, #229)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType


def _user(role="user", username="caller"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _vm_booking(user_id, created_by=None):
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id=user_id, status=BookingStatus.PENDING, resource_type=ResourceType.VM,
                   ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
                   image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
                   created_by=created_by)


def _client(current_user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: current_user
    return TestClient(app), app


# ── resolve_owner helper ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_resolve_owner_self_when_no_on_behalf():
    from app.presentation.routes._dispatch import resolve_owner
    u = _user()
    owner, created_by = await resolve_owner(AsyncMock(), u, None)
    assert owner == str(u.id) and created_by is None


@pytest.mark.asyncio
async def test_resolve_owner_dispatcher_maps_to_target():
    from app.presentation.routes._dispatch import resolve_owner
    disp = _user(role="dispatcher")
    target = _user(username="john@example.com")
    with patch("app.presentation.routes._dispatch._user_repo") as repo:
        repo.get_by_username = AsyncMock(return_value=target)
        owner, created_by = await resolve_owner(AsyncMock(), disp, "john@example.com")
    assert owner == str(target.id)
    assert created_by == str(disp.id)


@pytest.mark.asyncio
async def test_resolve_owner_normal_user_forbidden():
    from fastapi import HTTPException
    from app.presentation.routes._dispatch import resolve_owner
    with pytest.raises(HTTPException) as exc:
        await resolve_owner(AsyncMock(), _user(role="user"), "john@example.com")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_owner_unknown_target_400():
    from fastapi import HTTPException
    from app.presentation.routes._dispatch import resolve_owner
    with patch("app.presentation.routes._dispatch._user_repo") as repo:
        repo.get_by_username = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await resolve_owner(AsyncMock(), _user(role="admin"), "ghost")
    assert exc.value.status_code == 400


# ── POST /api/bookings on behalf ────────────────────────────────────────────────
def test_dispatcher_orders_vm_on_behalf():
    disp = _user(role="dispatcher")
    target = _user(username="john@example.com")
    cl, app = _client(disp)
    try:
        with patch("app.presentation.routes.api_bookings._resolve_catalog_id", new=AsyncMock(side_effect=[uuid4(), uuid4()])), \
             patch("app.presentation.routes.api_bookings._role_repo"), \
             patch("app.presentation.routes._dispatch._user_repo") as urepo, \
             patch("app.presentation.routes.api_bookings._create_use_case") as uc:
            urepo.get_by_username = AsyncMock(return_value=target)
            booking = _vm_booking(str(target.id), created_by=str(disp.id))
            uc.execute = AsyncMock(return_value=booking)
            resp = cl.post("/api/bookings", json={
                "resource_type": "VM", "ttl_minutes": 240,
                "image_name": "Ubuntu", "hw_config_name": "medium",
                "on_behalf_of": "john@example.com",
            })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    # The use case was called with the *target* as owner and the dispatcher as created_by.
    kwargs = uc.execute.call_args.kwargs
    assert kwargs["user_id"] == str(target.id)
    assert kwargs["created_by"] == str(disp.id)


def test_normal_user_on_behalf_forbidden():
    cl, app = _client(_user(role="user"))
    try:
        resp = cl.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240, "image_name": "U", "hw_config_name": "m",
            "on_behalf_of": "john@example.com",
        })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_self_order_has_no_created_by():
    user = _user(role="user")
    cl, app = _client(user)
    try:
        with patch("app.presentation.routes.api_bookings._resolve_catalog_id", new=AsyncMock(side_effect=[uuid4(), uuid4()])), \
             patch("app.presentation.routes.api_bookings._role_repo"), \
             patch("app.presentation.routes.api_bookings._create_use_case") as uc:
            uc.execute = AsyncMock(return_value=_vm_booking(str(user.id)))
            resp = cl.post("/api/bookings", json={
                "resource_type": "VM", "ttl_minutes": 240, "image_name": "U", "hw_config_name": "m",
            })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    kwargs = uc.execute.call_args.kwargs
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["created_by"] is None


# ── POST /api/environments on behalf ────────────────────────────────────────────
def test_dispatcher_orders_environment_on_behalf():
    from app.domain.entities import Environment
    disp = _user(role="dispatcher")
    target = _user(username="john@example.com")
    cl, app = _client(disp)
    now = datetime.now(timezone.utc)
    env = Environment(id=uuid4(), name="dev", blueprint_name="dev", user_id=str(target.id),
                      ttl_minutes=240, expires_at=now, created_at=now, bookings=[], created_by=str(disp.id))
    try:
        with patch("app.presentation.routes._dispatch._user_repo") as urepo, \
             patch("app.presentation.routes.api_environments._order_use_case") as uc:
            urepo.get_by_username = AsyncMock(return_value=target)
            uc.execute = AsyncMock(return_value=env)
            resp = cl.post("/api/environments", json={
                "blueprint_name": "dev", "ttl_minutes": 240, "on_behalf_of": "john@example.com",
            })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner_username"] == "john@example.com"
    assert body["created_by"] == str(disp.id)
    kwargs = uc.execute.call_args.kwargs
    assert kwargs["user_id"] == str(target.id) and kwargs["created_by"] == str(disp.id)


# ── created_by round-trips + serializes ─────────────────────────────────────────
def test_created_by_in_booking_summary():
    from app.presentation.routes.api_bookings import _summary
    b = _vm_booking("owner-id", created_by="dispatcher-id")
    assert _summary(b)["created_by"] == "dispatcher-id"
