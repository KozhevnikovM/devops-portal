"""Regression tests for D5/#299: QUEUED namespace adoption stalls environment lease.

A QUEUED namespace holds no resource; adopting it prevents start_lease_if_ready from ever
stamping the TTL. The use case must reject adoption of a QUEUED booking with 409.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.domain.entities import Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import NamespaceUnavailableError


def _bp_item(rt, spec, label=None, pos=0):
    return EnvironmentBlueprintItem(id=uuid4(), resource_type=rt, position=pos, label=label, spec=spec)


def _blueprint(items):
    return EnvironmentBlueprint(
        id=uuid4(), name="dev-stack", description=None, is_active=True,
        created_at=datetime.now(timezone.utc), items=items,
    )


def _booking(rt=ResourceType.NAMESPACE, status=BookingStatus.READY, env_id=None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, environment_id=env_id,
    )


def _make_use_case(blueprint, existing_ns_booking=None):
    env = Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
        ttl_minutes=240, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    env_repo = MagicMock()
    env_repo.create = AsyncMock(return_value=env)
    env_repo.get = AsyncMock(return_value=env)
    env_repo.delete = AsyncMock()
    env_repo.start_lease_if_ready = AsyncMock(return_value=False)
    blueprint_repo = MagicMock()
    blueprint_repo.get_by_name = AsyncMock(return_value=blueprint)
    booking_repo = MagicMock()
    booking_repo.update_status = AsyncMock()
    booking_repo.promote_next_queued = AsyncMock()
    booking_repo.get_live_standalone_namespace_booking = AsyncMock(return_value=existing_ns_booking)
    booking_repo.set_environment = AsyncMock()
    if existing_ns_booking is not None:
        booking_repo.get = AsyncMock(return_value=existing_ns_booking)
    create_uc = MagicMock()
    create_uc.execute = AsyncMock(side_effect=lambda *a, **k: _booking(ResourceType.VM, BookingStatus.PENDING, env.id))
    static_uc = MagicMock()
    static_uc.execute = AsyncMock(side_effect=lambda *a, **k: _booking(ResourceType.STATIC_VM, BookingStatus.READY, env.id))
    ns_uc = MagicMock()
    ns_uc.execute = AsyncMock(side_effect=lambda *a, **k: _booking(ResourceType.NAMESPACE, BookingStatus.READY, env.id))
    image_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    hw_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    role_repo = MagicMock(get_by_name=AsyncMock(return_value=None))
    svm_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    dispatcher = MagicMock()
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, booking_repo, create_uc, static_uc, ns_uc,
        image_repo, hw_repo, role_repo, svm_repo, dispatcher,
    )
    return uc, SimpleNamespace(env=env, env_repo=env_repo, booking_repo=booking_repo, ns_uc=ns_uc)


@pytest.mark.asyncio
async def test_queued_namespace_adoption_raises_namespace_unavailable():
    """Adopting a QUEUED standalone namespace must raise NamespaceUnavailableError (→ 409)."""
    ns_id = uuid4()
    queued_booking = _booking(status=BookingStatus.QUEUED)
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case(bp, existing_ns_booking=queued_booking)

    session = AsyncMock()
    with pytest.raises(NamespaceUnavailableError) as exc_info:
        await uc.execute(session, "dev-stack", 240, user_id="u", namespace_id=ns_id)

    assert "booking queue" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_queued_namespace_adoption_creates_no_environment_row():
    """No environment row must be persisted when the adoption guard fires."""
    ns_id = uuid4()
    queued_booking = _booking(status=BookingStatus.QUEUED)
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case(bp, existing_ns_booking=queued_booking)

    session = AsyncMock()
    with pytest.raises(NamespaceUnavailableError):
        await uc.execute(session, "dev-stack", 240, user_id="u", namespace_id=ns_id)

    # Guard fires before any environment row is created
    m.env_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_namespace_adoption_succeeds():
    """Adopting a READY standalone namespace must still succeed (no regression)."""
    ns_id = uuid4()
    ready_booking = _booking(status=BookingStatus.READY)
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case(bp, existing_ns_booking=ready_booking)

    session = AsyncMock()
    env = await uc.execute(session, "dev-stack", 240, user_id="u", namespace_id=ns_id)

    assert env is not None
    m.booking_repo.set_environment.assert_awaited_once()
    m.ns_uc.execute.assert_not_awaited()
