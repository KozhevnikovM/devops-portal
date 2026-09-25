"""#434 — the environment lease starts once every child has settled, exactly once, from every trigger.

Fast (mocked) coverage. The row-lock behaviour under real concurrency, the reconciliation query and
the crash-gap scenarios run against Postgres in tests/integration/test_environment_lease_*.py.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import SecretDecryptionError
from app.infrastructure.database.models import EnvironmentModel
from app.infrastructure.repositories.environment_repo import EnvironmentRepository


def _child(status: BookingStatus):
    return SimpleNamespace(status=status.value, expires_at=PERMANENT_EXPIRES_AT)


def _sync_session(children, ttl_minutes=240, expires_at=PERMANENT_EXPIRES_AT, construction_complete=True):
    env = SimpleNamespace(id=uuid4(), ttl_minutes=ttl_minutes, expires_at=expires_at,
                          construction_complete=construction_complete)
    session = MagicMock()
    session.get.return_value = env
    session.execute.return_value.scalars.return_value.all.return_value = children
    return session, env


def _within(deadline, minutes):
    now = datetime.now(timezone.utc)
    return now + timedelta(minutes=minutes - 1) < deadline < now + timedelta(minutes=minutes + 1)


# ── 4.1 the stamp itself ──────────────────────────────────────────────────────────────────────
def test_ready_plus_failed_starts_the_lease_under_a_row_lock():
    children = [_child(BookingStatus.READY), _child(BookingStatus.FAILED)]
    session, env = _sync_session(children)
    assert EnvironmentRepository().sync_start_lease_if_ready(session, env.id) is True
    assert _within(env.expires_at, 240)
    assert all(c.expires_at == env.expires_at for c in children)
    # Locked (and re-read) before deciding; committed to release the lock.
    session.get.assert_called_once_with(
        EnvironmentModel, env.id, with_for_update=True, populate_existing=True,
    )
    session.commit.assert_called_once()


def test_in_flight_child_holds_the_lease_back():
    session, env = _sync_session([_child(BookingStatus.READY), _child(BookingStatus.PROVISIONING)])
    assert EnvironmentRepository().sync_start_lease_if_ready(session, env.id) is False
    assert env.expires_at == PERMANENT_EXPIRES_AT
    session.commit.assert_called_once()  # the lock is released even when nothing is stamped


def test_started_lease_is_not_moved_by_a_later_trigger():
    started = datetime.now(timezone.utc) + timedelta(minutes=5)
    children = [_child(BookingStatus.READY), _child(BookingStatus.FAILED)]
    session, env = _sync_session(children, expires_at=started)
    assert EnvironmentRepository().sync_start_lease_if_ready(session, env.id) is False
    assert env.expires_at == started


def test_permanent_environment_stays_permanent():
    session, env = _sync_session([_child(BookingStatus.READY)], ttl_minutes=0)
    EnvironmentRepository().sync_start_lease_if_ready(session, env.id)
    assert env.expires_at == PERMANENT_EXPIRES_AT


def test_missing_environment_is_a_no_op():
    session = MagicMock()
    session.get.return_value = None
    assert EnvironmentRepository().sync_start_lease_if_ready(session, uuid4()) is False


@pytest.mark.asyncio
async def test_async_path_locks_and_stamps():
    children = [_child(BookingStatus.READY), _child(BookingStatus.RELEASING)]
    env = SimpleNamespace(id=uuid4(), ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT, construction_complete=True)
    session = AsyncMock()
    session.get = AsyncMock(return_value=env)
    result = MagicMock()
    result.scalars.return_value.all.return_value = children
    session.execute = AsyncMock(return_value=result)
    assert await EnvironmentRepository().start_lease_if_ready(session, env.id) is True
    assert _within(env.expires_at, 60)
    session.get.assert_awaited_once_with(
        EnvironmentModel, env.id, with_for_update=True, populate_existing=True,
    )
    session.commit.assert_awaited_once()


# ── 4.2 async by-booking entry point ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_async_for_booking_is_a_no_op_for_standalone():
    session = AsyncMock()
    session.get = AsyncMock(return_value=SimpleNamespace(environment_id=None))
    repo = EnvironmentRepository()
    with patch.object(repo, "start_lease_if_ready", AsyncMock()) as start:
        assert await repo.start_lease_if_ready_for_booking(session, uuid4()) is False
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_for_booking_checks_the_childs_environment():
    env_id = uuid4()
    session = AsyncMock()
    session.get = AsyncMock(return_value=SimpleNamespace(environment_id=env_id))
    repo = EnvironmentRepository()
    with patch.object(repo, "start_lease_if_ready", AsyncMock(return_value=True)) as start:
        assert await repo.start_lease_if_ready_for_booking(session, uuid4()) is True
    start.assert_awaited_once_with(session, env_id)


# ── 4.3 provision: final FAILED is a settling event ───────────────────────────────────────────
def _run_provision(asyncio_side_effect):
    from app.domain.entities import HWConfig, VMImage
    now = datetime.now(timezone.utc)
    repo = MagicMock()
    repo.sync_get = MagicMock(return_value=MagicMock(status=BookingStatus.PENDING))
    image_repo = MagicMock()
    image_repo.sync_get = MagicMock(return_value=VMImage(
        id=uuid4(), name="img", vapp_template_id="tpl", is_active=True, created_at=now))
    hw_repo = MagicMock()
    hw_repo.sync_get = MagicMock(return_value=HWConfig(
        id=uuid4(), name="hw", cpus=1, memory_mb=1024, disk_mb=10240, is_active=True, created_at=now))
    env_repo = MagicMock()
    booking_id = str(uuid4())
    with (
        patch("app.tasks.provision.SyncSessionLocal") as factory,
        patch("app.tasks.provision.repo", repo),
        patch("app.tasks.provision.image_repo", image_repo),
        patch("app.tasks.provision.hw_config_repo", hw_repo),
        patch("app.tasks.provision.env_repo", env_repo),
        patch("app.tasks.provision.asyncio.run", side_effect=asyncio_side_effect),
    ):
        factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        factory.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, str(uuid4()), str(uuid4())])
    return repo, env_repo, booking_id


def test_provision_final_failure_checks_the_environment_lease():
    repo, env_repo, booking_id = _run_provision(RuntimeError("boom"))
    statuses = [c.args[2] for c in repo.sync_update_status.call_args_list]
    assert statuses[-1] == BookingStatus.FAILED and BookingStatus.RETRY in statuses
    # Checked once — on the final FAILED only, not on each RETRY.
    env_repo.sync_start_lease_if_ready_for_booking.assert_called_once()
    assert str(env_repo.sync_start_lease_if_ready_for_booking.call_args.args[1]) == booking_id


def test_provision_secret_decryption_failure_checks_the_environment_lease():
    repo, env_repo, booking_id = _run_provision(SecretDecryptionError("bad key"))
    assert repo.sync_update_status.call_args_list[-1].args[2] == BookingStatus.FAILED
    env_repo.sync_start_lease_if_ready_for_booking.assert_called_once()
    assert str(env_repo.sync_start_lease_if_ready_for_booking.call_args.args[1]) == booking_id


# ── 4.4 stale reaper ──────────────────────────────────────────────────────────────────────────
def test_reaper_checks_the_environment_lease_after_each_failure():
    stale = [SimpleNamespace(id=uuid4(), status=BookingStatus.PROVISIONING) for _ in range(2)]
    repo = MagicMock()
    repo.sync_list_stale_provisioning.return_value = stale
    env_repo = MagicMock()
    manager = MagicMock()
    manager.attach_mock(repo.sync_update_status, "update")
    manager.attach_mock(env_repo.sync_start_lease_if_ready_for_booking, "lease")
    with patch("app.tasks.beat_tasks.SyncSessionLocal"), \
         patch("app.tasks.beat_tasks.repo", repo), \
         patch("app.tasks.beat_tasks.env_repo", env_repo):
        from app.tasks.beat_tasks import reap_stale_provisioning
        reap_stale_provisioning()
    names = [c[0] for c in manager.mock_calls]
    assert names == ["update", "lease", "update", "lease"]
    assert [c.args[1] for c in env_repo.sync_start_lease_if_ready_for_booking.call_args_list] == \
        [b.id for b in stale]


# ── 4.5 promotion: checked only after the promotion commits ───────────────────────────────────
def _queued(environment_id):
    return SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240, static_vm_id=None,
        namespace_id=None, expires_at=None, environment_id=environment_id,
        user_id="owner-1", created_by=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("environment_id", [uuid4(), None])
async def test_async_promotion_checks_the_lease_after_commit(environment_id):
    from app.infrastructure.repositories import booking_repo as mod
    queued = _queued(environment_id)
    session = AsyncMock()
    session.add = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: SimpleNamespace(id=uuid4(), name="vm-1")
    session.execute = AsyncMock(side_effect=[r1, r2])
    env_repo = MagicMock(start_lease_if_ready=AsyncMock(return_value=True))
    manager = MagicMock()
    manager.attach_mock(session.commit, "commit")
    manager.attach_mock(env_repo.start_lease_if_ready, "lease")
    with patch.object(mod, "_to_entity", lambda m: m), \
         patch.object(mod, "_environment_repo", return_value=env_repo):
        await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)
    if environment_id is None:
        env_repo.start_lease_if_ready.assert_not_awaited()
    else:
        assert manager.mock_calls == [call.commit(), call.lease(session, environment_id)]


@pytest.mark.parametrize("environment_id", [uuid4(), None])
def test_sync_promotion_checks_the_lease_after_commit(environment_id):
    from app.infrastructure.repositories import booking_repo as mod
    queued = _queued(environment_id)
    session = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: SimpleNamespace(id=uuid4(), name="vm-1")
    session.execute = MagicMock(side_effect=[r1, r2])
    env_repo = MagicMock()
    manager = MagicMock()
    manager.attach_mock(session.commit, "commit")
    manager.attach_mock(env_repo.sync_start_lease_if_ready, "lease")
    with patch.object(mod, "_to_entity", lambda m: m), \
         patch.object(mod, "_environment_repo", return_value=env_repo):
        mod.BookingRepository().sync_promote_next_queued(session, ResourceType.STATIC_VM.value)
    if environment_id is None:
        env_repo.sync_start_lease_if_ready.assert_not_called()
    else:
        assert manager.mock_calls == [call.commit(), call.lease(session, environment_id)]


# ── 4.7 reconciliation beat task ──────────────────────────────────────────────────────────────
def test_reconcile_starts_each_pending_lease_and_survives_a_failure():
    pending = [uuid4(), uuid4(), uuid4()]
    env_repo = MagicMock()
    env_repo.sync_list_lease_pending.return_value = pending
    env_repo.sync_start_lease_if_ready.side_effect = [True, RuntimeError("db hiccup"), True]
    with patch("app.tasks.beat_tasks.SyncSessionLocal"), \
         patch("app.tasks.beat_tasks.env_repo", env_repo):
        from app.tasks.beat_tasks import reconcile_environment_leases
        reconcile_environment_leases()
    assert [c.args[1] for c in env_repo.sync_start_lease_if_ready.call_args_list] == pending


def test_reconcile_is_on_the_beat_schedule():
    from app.config import settings
    from app.infrastructure.celery_app import celery_app
    entry = celery_app.conf.beat_schedule["reconcile-environment-leases"]
    assert entry["task"] == "app.tasks.beat_tasks.reconcile_environment_leases"
    assert entry["schedule"] == settings.ENFORCE_TTL_INTERVAL_SECONDS


# ── 4.10 / 4.11 construction-complete marker: nothing stamps a half-built environment ─────────
def test_incomplete_environment_never_stamps_even_when_settled():
    children = [_child(BookingStatus.READY)]
    session, env = _sync_session(children, construction_complete=False)
    assert EnvironmentRepository().sync_start_lease_if_ready(session, env.id) is False
    assert env.expires_at == PERMANENT_EXPIRES_AT
    session.commit.assert_called_once()  # lock still released


@pytest.mark.asyncio
async def test_incomplete_environment_never_stamps_async():
    env = SimpleNamespace(id=uuid4(), ttl_minutes=60, expires_at=PERMANENT_EXPIRES_AT,
                          construction_complete=False)
    session = AsyncMock()
    session.get = AsyncMock(return_value=env)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_child(BookingStatus.READY)]
    session.execute = AsyncMock(return_value=result)
    assert await EnvironmentRepository().start_lease_if_ready(session, env.id) is False
    assert env.expires_at == PERMANENT_EXPIRES_AT


def _order_use_case(items, child_results):
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    from app.domain.entities import (
        Environment,
        EnvironmentBlueprint,
        EnvironmentBlueprintItem,
    )
    now = datetime.now(timezone.utc)
    bp = EnvironmentBlueprint(
        id=uuid4(), name="bp", description=None, is_active=True, created_at=now,
        items=[EnvironmentBlueprintItem(id=uuid4(), resource_type="NAMESPACE", position=i,
                                        label=f"ns{i}", spec={}) for i in range(items)],
    )
    env = Environment(id=uuid4(), name="bp", blueprint_name="bp", user_id="u", ttl_minutes=60,
                      expires_at=PERMANENT_EXPIRES_AT, created_at=now)
    env_repo = MagicMock(create=AsyncMock(return_value=env), get=AsyncMock(return_value=env),
                         delete=AsyncMock(), start_lease_if_ready=AsyncMock(return_value=True),
                         mark_construction_complete=AsyncMock())
    ns_uc = MagicMock(execute=AsyncMock(side_effect=child_results))
    booking_repo = MagicMock(update_status=AsyncMock(), promote_next_queued=AsyncMock())
    uc = OrderEnvironmentUseCase(
        env_repo, MagicMock(get_by_name=AsyncMock(return_value=bp)), booking_repo, MagicMock(),
        MagicMock(), ns_uc, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )
    manager = MagicMock()
    manager.attach_mock(ns_uc.execute, "create_child")
    manager.attach_mock(env_repo.mark_construction_complete, "mark")
    manager.attach_mock(env_repo.start_lease_if_ready, "lease")
    return uc, env, env_repo, manager


def _ns_booking():
    from app.domain.entities import Booking
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id="u", status=BookingStatus.READY,
                   resource_type=ResourceType.NAMESPACE, ttl_minutes=60,
                   expires_at=PERMANENT_EXPIRES_AT, created_at=now)


@pytest.mark.asyncio
async def test_order_marks_construction_complete_after_every_child_before_the_lease():
    uc, env, env_repo, manager = _order_use_case(2, [_ns_booking(), _ns_booking()])
    await uc.execute(MagicMock(), "bp", 60, user_id="u")
    names = [c[0] for c in manager.mock_calls]
    assert names == ["create_child", "create_child", "mark", "lease"]
    env_repo.mark_construction_complete.assert_awaited_once()
    assert env_repo.mark_construction_complete.await_args.args[1] == env.id


@pytest.mark.asyncio
async def test_failed_order_is_never_marked_complete():
    uc, _, env_repo, _ = _order_use_case(2, [_ns_booking(), RuntimeError("second child failed")])
    with pytest.raises(RuntimeError):
        await uc.execute(MagicMock(), "bp", 60, user_id="u")
    env_repo.mark_construction_complete.assert_not_awaited()
    env_repo.start_lease_if_ready.assert_not_awaited()
    env_repo.delete.assert_awaited_once()  # rolled back
