"""Regression tests for #223 — the lease (TTL) starts when a resource is READY, not at creation."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus


# ── Standalone booking: sync_update_status(start_lease=True) ─────────────────────
def _fake_model(status="PROVISIONING", ttl_minutes=240):
    return SimpleNamespace(
        status=status, ttl_minutes=ttl_minutes, expires_at=PERMANENT_EXPIRES_AT,
        vm_ip=None, vm_password=None, config_failed=False, id=uuid4(),
    )


def test_start_lease_stamps_expiry_on_ready():
    from app.infrastructure.repositories.booking_repo import BookingRepository
    model = _fake_model(ttl_minutes=240)
    session = MagicMock(); session.get.return_value = model
    before = datetime.now(timezone.utc)
    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY, start_lease=True)
    # expires_at is now ~ now + 240 min, NOT the creation-time placeholder.
    assert before + timedelta(minutes=239) < model.expires_at < before + timedelta(minutes=241)


def test_start_lease_permanent_stays_permanent():
    from app.infrastructure.repositories.booking_repo import BookingRepository
    model = _fake_model(ttl_minutes=0)
    session = MagicMock(); session.get.return_value = model
    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY, start_lease=True)
    assert model.expires_at == PERMANENT_EXPIRES_AT


def test_no_start_lease_leaves_expiry_untouched():
    from app.infrastructure.repositories.booking_repo import BookingRepository
    model = _fake_model(ttl_minutes=240)
    original = model.expires_at
    session = MagicMock(); session.get.return_value = model
    # A non-READY transition, or start_lease=False, must not touch expires_at.
    BookingRepository().sync_update_status(session, model.id, BookingStatus.PROVISIONING, start_lease=True)
    assert model.expires_at == original
    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY, start_lease=False)
    assert model.expires_at == original


# ── Environment: whole-stack lease starts when all children READY ────────────────
def _env_session(children, ttl_minutes=240):
    """A MagicMock session where get() returns a booking then its env, and execute() the children."""
    env = SimpleNamespace(id=uuid4(), ttl_minutes=ttl_minutes, expires_at=PERMANENT_EXPIRES_AT)
    booking = SimpleNamespace(id=uuid4(), environment_id=env.id)
    session = MagicMock()
    session.get.side_effect = lambda model, id_: booking if id_ == booking.id else env
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = children
    session.execute.return_value = exec_result
    return session, env, booking


def test_env_lease_starts_when_all_children_ready():
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository
    c1 = SimpleNamespace(status=BookingStatus.READY.value, expires_at=PERMANENT_EXPIRES_AT)
    c2 = SimpleNamespace(status=BookingStatus.READY.value, expires_at=PERMANENT_EXPIRES_AT)
    session, env, booking = _env_session([c1, c2], ttl_minutes=240)
    before = datetime.now(timezone.utc)
    stamped = EnvironmentRepository().sync_start_lease_if_ready_for_booking(session, booking.id)
    assert stamped is True
    # Env and every child share one real deadline.
    assert before + timedelta(minutes=239) < env.expires_at < before + timedelta(minutes=241)
    assert c1.expires_at == env.expires_at == c2.expires_at


def test_env_lease_not_started_while_a_child_in_flight():
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository
    c1 = SimpleNamespace(status=BookingStatus.READY.value, expires_at=PERMANENT_EXPIRES_AT)
    c2 = SimpleNamespace(status=BookingStatus.PROVISIONING.value, expires_at=PERMANENT_EXPIRES_AT)
    session, env, booking = _env_session([c1, c2])
    stamped = EnvironmentRepository().sync_start_lease_if_ready_for_booking(session, booking.id)
    assert stamped is False
    assert env.expires_at == PERMANENT_EXPIRES_AT  # untouched


def test_standalone_booking_is_not_an_env_lease():
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository
    booking = SimpleNamespace(id=uuid4(), environment_id=None)
    session = MagicMock(); session.get.return_value = booking
    assert EnvironmentRepository().sync_start_lease_if_ready_for_booking(session, booking.id) is False


# ── Ordering stamps the lease immediately for an all-pooled (ready-at-once) env ──
@pytest.mark.asyncio
async def test_order_calls_start_lease_after_children():
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    from app.domain.entities import EnvironmentBlueprint, EnvironmentBlueprintItem, Environment, Booking
    from app.domain.enums import ResourceType
    bp = EnvironmentBlueprint(id=uuid4(), name="ns-only", description=None, is_active=True,
                              created_at=datetime.now(timezone.utc),
                              items=[EnvironmentBlueprintItem(id=uuid4(), resource_type="NAMESPACE",
                                                             position=0, label="ns", spec={})])
    env = Environment(id=uuid4(), name="ns-only", blueprint_name="ns-only", user_id="u",
                      ttl_minutes=240, expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc))
    ns_booking = Booking(id=uuid4(), user_id="u", status=BookingStatus.READY,
                         resource_type=ResourceType.NAMESPACE, ttl_minutes=240,
                         expires_at=PERMANENT_EXPIRES_AT, created_at=datetime.now(timezone.utc))
    env_repo = MagicMock(create=AsyncMock(return_value=env), get=AsyncMock(return_value=env),
                         start_lease_if_ready=AsyncMock(return_value=True))
    blueprint_repo = MagicMock(get_by_name=AsyncMock(return_value=bp))
    ns_uc = MagicMock(execute=AsyncMock(return_value=ns_booking))
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, MagicMock(), MagicMock(), MagicMock(), ns_uc,
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    await uc.execute(MagicMock(), "ns-only", 240, user_id="u")
    # The env is created with a placeholder, then asked to start its lease (all children READY).
    env_repo.start_lease_if_ready.assert_awaited_once()
    assert env_repo.start_lease_if_ready.await_args.args[1] == env.id
