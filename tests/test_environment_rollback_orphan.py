"""Regression tests for #292: _rollback orphaned PENDING VM children.

Before the fix, PENDING → RELEASED was not in ALLOWED_TRANSITIONS, so the bare
`except Exception: pass` in _rollback swallowed the IllegalStatusTransitionError and the
VM children were detached from the environment (ON DELETE SET NULL) but never released —
becoming orphaned PENDING bookings that counted against quota forever.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from app.domain.booking_status import ALLOWED_TRANSITIONS, can_transition
from app.domain.entities import Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import QuotaExceededError


# ── Transition map ────────────────────────────────────────────────────────────


def test_pending_to_released_is_now_allowed():
    """PENDING → RELEASED must be valid so rollback can release never-dispatched VM children."""
    assert can_transition(BookingStatus.PENDING, BookingStatus.RELEASED)
    assert BookingStatus.RELEASED in ALLOWED_TRANSITIONS[BookingStatus.PENDING]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bp_item(rt, spec, label=None, pos=0):
    return EnvironmentBlueprintItem(id=uuid4(), resource_type=rt, position=pos, label=label, spec=spec)


def _blueprint(items):
    return EnvironmentBlueprint(
        id=uuid4(), name="dev-stack", description=None, is_active=True,
        created_at=datetime.now(timezone.utc), items=items,
    )


def _booking(rt=ResourceType.VM, status=BookingStatus.PENDING, env_id=None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, environment_id=env_id,
    )


def _make_use_case(blueprint, *, create_side_effect=None, static_returns=None, ns_returns=None):
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
    booking_repo.promote_next_queued = AsyncMock(return_value=None)
    booking_repo.get_live_standalone_namespace_booking = AsyncMock(return_value=None)

    if create_side_effect is not None:
        create_uc = MagicMock()
        create_uc.execute = AsyncMock(side_effect=create_side_effect)
    else:
        create_uc = MagicMock()
        create_uc.execute = AsyncMock(side_effect=lambda *a, **k: _booking(env_id=env.id))

    static_uc = MagicMock()
    static_uc.execute = AsyncMock(
        side_effect=static_returns or (lambda *a, **k: _booking(ResourceType.STATIC_VM, BookingStatus.READY, env.id))
    )
    ns_uc = MagicMock()
    ns_uc.execute = AsyncMock(
        side_effect=ns_returns or (lambda *a, **k: _booking(ResourceType.NAMESPACE, BookingStatus.READY, env.id))
    )

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
    return uc, SimpleNamespace(
        env=env, env_repo=env_repo, booking_repo=booking_repo, dispatcher=dispatcher,
    )


# ── Regression: PENDING VM child must be released on rollback ─────────────────


@pytest.mark.asyncio
async def test_rollback_releases_pending_vm_child():
    """A VM child is created PENDING; if a later item fails, rollback must release it."""
    vm_booking = _booking(ResourceType.VM, BookingStatus.PENDING)

    calls = [vm_booking, QuotaExceededError("quota")]

    async def create_side(session, *a, **k):
        val = calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    bp = _blueprint([
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm1", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm2", 1),
    ])
    uc, m = _make_use_case(bp, create_side_effect=create_side)

    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    # The first VM (PENDING) must be RELEASED — not left orphaned.
    m.booking_repo.update_status.assert_awaited_once_with(
        ANY, vm_booking.id, BookingStatus.RELEASED
    )
    m.env_repo.delete.assert_awaited_once()
    m.dispatcher.dispatch_provision.assert_not_called()


@pytest.mark.asyncio
async def test_rollback_releases_all_pending_children():
    """When two VM children are created before failure, both are released."""
    vm1 = _booking(ResourceType.VM, BookingStatus.PENDING)
    vm2 = _booking(ResourceType.VM, BookingStatus.PENDING)

    calls = [vm1, vm2, QuotaExceededError("quota")]

    async def create_side(session, *a, **k):
        val = calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    bp = _blueprint([
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm1", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm2", 1),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm3", 2),
    ])
    uc, m = _make_use_case(bp, create_side_effect=create_side)

    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    released_ids = {c.args[1] for c in m.booking_repo.update_status.await_args_list}
    assert vm1.id in released_ids
    assert vm2.id in released_ids


@pytest.mark.asyncio
async def test_rollback_continues_after_release_failure():
    """If one child's release raises, rollback logs and continues releasing the rest."""
    vm1 = _booking(ResourceType.VM, BookingStatus.PENDING)
    vm2 = _booking(ResourceType.VM, BookingStatus.PENDING)

    create_calls = [vm1, vm2, RuntimeError("db error")]

    async def create_side(session, *a, **k):
        val = create_calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    release_calls = iter([RuntimeError("release failed"), None])

    async def update_status_side(session, bid, status):
        val = next(release_calls)
        if isinstance(val, Exception):
            raise val

    bp = _blueprint([
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm1", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm2", 1),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm3", 2),
    ])
    uc, m = _make_use_case(bp, create_side_effect=create_side)
    m.booking_repo.update_status = AsyncMock(side_effect=update_status_side)

    with patch("app.application.use_cases.order_environment.logger") as mock_log:
        with pytest.raises(RuntimeError, match="db error"):
            await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    # logger.exception must have been called (not silently swallowed).
    assert mock_log.exception.called
    # env delete still attempted.
    m.env_repo.delete.assert_awaited_once()


# ── promote_next_queued called for pool types ─────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_promotes_queued_after_static_vm_freed():
    """Releasing a STATIC_VM child triggers promote_next_queued for that resource type."""
    static_booking = _booking(ResourceType.STATIC_VM, BookingStatus.READY)

    calls = [static_booking, QuotaExceededError("quota")]

    async def ns_side(session, *a, **k):
        val = calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    bp = _blueprint([
        _bp_item("STATIC_VM", {}, "svm", 0),
        _bp_item("NAMESPACE", {}, "ns", 1),
    ])
    uc, m = _make_use_case(bp)
    uc._reserve_static.execute = AsyncMock(side_effect=ns_side)
    uc._book_namespace.execute = AsyncMock(side_effect=QuotaExceededError("quota"))

    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    called_types = {c.args[1] for c in m.booking_repo.promote_next_queued.await_args_list}
    assert ResourceType.STATIC_VM.value in called_types


@pytest.mark.asyncio
async def test_rollback_no_promote_for_vm_type():
    """VM children are not pooled — rollback must NOT call promote_next_queued for VM type."""
    vm_booking = _booking(ResourceType.VM, BookingStatus.PENDING)

    calls = [vm_booking, QuotaExceededError("quota")]

    async def create_side(session, *a, **k):
        val = calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    bp = _blueprint([
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm1", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "vm2", 1),
    ])
    uc, m = _make_use_case(bp, create_side_effect=create_side)

    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    called_types = [c.args[1] for c in m.booking_repo.promote_next_queued.await_args_list]
    assert ResourceType.VM.value not in called_types
