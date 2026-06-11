"""Tests for blueprint item labels on environment resources (#224)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import (
    Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem, User,
)
from app.domain.enums import BookingStatus, ResourceType


def _item(rt, spec, label, pos):
    return EnvironmentBlueprintItem(id=uuid4(), resource_type=rt, position=pos, label=label, spec=spec)


def _booking(rt=ResourceType.VM, status=BookingStatus.PROVISIONING, label=None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="0", status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
        environment_label=label, image_name="Ubuntu",
    )


# ── Ordering carries the item label onto each child ─────────────────────────────
@pytest.mark.asyncio
async def test_order_passes_item_label_to_children():
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    bp = EnvironmentBlueprint(
        id=uuid4(), name="dev-stack", description=None, is_active=True,
        created_at=datetime.now(timezone.utc),
        items=[
            _item("NAMESPACE", {}, "ns", 0),
            _item("VM", {"image_name": "U", "hw_config_name": "m"}, "web", 1),
        ],
    )
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=datetime.now(timezone.utc),
                      created_at=datetime.now(timezone.utc))
    env_repo = MagicMock(create=AsyncMock(return_value=env), get=AsyncMock(return_value=env),
                         start_lease_if_ready=AsyncMock(return_value=False))
    blueprint_repo = MagicMock(get_by_name=AsyncMock(return_value=bp))
    create_uc = MagicMock(execute=AsyncMock(return_value=_booking()))
    ns_uc = MagicMock(execute=AsyncMock(return_value=_booking(ResourceType.NAMESPACE, BookingStatus.READY)))
    static_uc = MagicMock(execute=AsyncMock())
    image_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    hw_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    role_repo = MagicMock(get_by_name=AsyncMock())
    svm_repo = MagicMock(get_by_name=AsyncMock())
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, MagicMock(), create_uc, static_uc, ns_uc,
        image_repo, hw_repo, role_repo, svm_repo, MagicMock(),
    )
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    assert ns_uc.execute.call_args.kwargs["environment_label"] == "ns"
    assert create_uc.execute.call_args.kwargs["environment_label"] == "web"


# ── Use cases persist the label on the booking ──────────────────────────────────
@pytest.mark.asyncio
async def test_create_booking_persists_label():
    from app.application.use_cases.create_booking import CreateBookingUseCase
    repo = MagicMock(create=AsyncMock(side_effect=lambda s, b: b))
    image_repo = MagicMock(get=AsyncMock(return_value=SimpleNamespace(id=uuid4(), name="U")))
    hw_repo = MagicMock(get=AsyncMock(return_value=SimpleNamespace(
        id=uuid4(), name="m", cpus=1, memory_mb=1024, disk_mb=1024, drive_type="HDD")))
    quota = MagicMock(get_limits_for_update=AsyncMock(return_value={
        "max_cpus": 99, "max_memory_gb": 99, "max_ssd_gb": 99, "max_hdd_gb": 99}),
        count_active_resources=AsyncMock(return_value={
            "cpus": 0, "memory_gb": 0, "ssd_gb": 0, "hdd_gb": 0}))
    uc = CreateBookingUseCase(repo, image_repo, hw_repo, quota_repo=quota, dispatcher=MagicMock())
    booking = await uc.execute(MagicMock(), 240, uuid4(), uuid4(), user_id="u",
                               environment_label="web", dispatch=False)
    assert booking.environment_label == "web"


# ── UI renders the label, falling back to the type ──────────────────────────────
def _client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    admin = User(id=UUID(int=0), username="admin", password_hash="", role="admin",
                 is_active=True, created_at=datetime.now(timezone.utc))
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: admin
    return TestClient(app), app


def _env(children):
    now = datetime.now(timezone.utc)
    return Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack",
                       user_id=str(UUID(int=0)), ttl_minutes=240, expires_at=now + timedelta(minutes=240),
                       created_at=now, bookings=children, owner_username="admin")


def test_row_renders_label_with_fallback():
    cl, app = _client()
    env = _env([
        _booking(ResourceType.VM, BookingStatus.PROVISIONING, label="web"),
        _booking(ResourceType.NAMESPACE, BookingStatus.READY, label=None),  # falls back to type
    ])
    try:
        with patch("app.presentation.routes.environments._env_repo") as er:
            er.get = AsyncMock(return_value=env)
            resp = cl.get(f"/environments/{env.id}/row")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert ">web<" in resp.text or "web" in resp.text     # the VM's label
    assert "namespace" in resp.text                       # fallback for the unlabeled child


# ── API child summary includes the label ────────────────────────────────────────
def test_api_child_includes_label():
    cl, app = _client()
    env = _env([_booking(ResourceType.VM, BookingStatus.PROVISIONING, label="web")])
    try:
        with patch("app.presentation.routes.api_environments._env_repo") as er:
            er.get = AsyncMock(return_value=env)
            resp = cl.get(f"/api/environments/{env.id}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["bookings"][0]["label"] == "web"
