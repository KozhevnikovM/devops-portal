"""Integration: nothing starts the lease of a half-built environment (#434, PR #470 review).

Ordering creates the environment and then its children one at a time, and each child commits, so a
partly built environment is visible to other workers. After its first READY pooled (or adopted)
child it *looks* settled — the later children don't exist yet. The construction-complete marker
keeps reconciliation and queue promotion from stamping the lease until the order has created
every child.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentBlueprintItemModel,
    EnvironmentBlueprintModel,
    HWConfigModel,
    NamespaceModel,
    QuotaModel,
    VMImageModel,
)
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.presentation import deps
from tests.integration._environment_lease import (
    cleanup,
    expiries,
    insert_environment,
    make_sessionmaker,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]

_TTL = 60


def _reconcile(Session):
    from app.tasks.beat_tasks import reconcile_environment_leases
    with patch("app.tasks.beat_tasks.SyncSessionLocal", Session):
        reconcile_environment_leases()


async def _seed_catalog(async_engine, tag):
    ids = {"image": uuid4(), "hw": uuid4(), "ns": uuid4(), "bp": uuid4()}
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        s.add(VMImageModel(id=ids["image"], name=f"img-{tag}", vapp_template_id="tpl", is_active=True))
        s.add(HWConfigModel(id=ids["hw"], name=f"hw-{tag}", cpus=1, memory_mb=1024, disk_mb=10240,
                            drive_type="HDD", is_active=True))
        s.add(NamespaceModel(id=ids["ns"], name=f"ns-{tag}", cluster_name=f"cl-{tag}",
                             api_url=None, is_active=True))
        bp = EnvironmentBlueprintModel(id=ids["bp"], name=f"bp-{tag}", description=None, is_active=True)
        bp.items = [
            EnvironmentBlueprintItemModel(resource_type="NAMESPACE", position=0, label="ns",
                                          spec={"namespace_name": f"ns-{tag}", "cluster_name": f"cl-{tag}"}),
            EnvironmentBlueprintItemModel(resource_type="VM", position=1, label="web",
                                          spec={"image_name": f"img-{tag}", "hw_config_name": f"hw-{tag}"}),
        ]
        s.add(bp)
        await s.commit()
    return ids


async def _drop_catalog(async_engine, ids, user_id):
    async with AsyncSession(async_engine) as s:
        await s.execute(delete(EnvironmentBlueprintModel).where(EnvironmentBlueprintModel.id == ids["bp"]))
        await s.execute(delete(NamespaceModel).where(NamespaceModel.id == ids["ns"]))
        await s.execute(delete(VMImageModel).where(VMImageModel.id == ids["image"]))
        await s.execute(delete(HWConfigModel).where(HWConfigModel.id == ids["hw"]))
        await s.execute(delete(QuotaModel).where(QuotaModel.user_id == user_id))
        await s.commit()


async def _order_pausing_after_first_child(async_engine, Session, ids, user_id, **order_kwargs):
    """Order the blueprint; right after the first child is committed, run reconciliation from
    another session and record the environment's expiry at that moment."""
    uc = deps.order_environment_uc
    original = uc._create_child
    seen: dict = {}

    async def create_child_then_reconcile(session, item, res, ttl, uid, env_id, *args, **kwargs):
        booking = await original(session, item, res, ttl, uid, env_id, *args, **kwargs)
        if "mid_order_expiry" not in seen:
            seen["first_child_status"] = booking.status
            _reconcile(Session)  # another worker's beat tick lands mid-order
            seen["mid_order_expiry"] = expiries(Session, env_id)[0]
        return booking

    with patch.object(uc, "_create_child", create_child_then_reconcile), \
         patch.object(uc, "_dispatcher", MagicMock()):
        async with AsyncSession(async_engine, expire_on_commit=False) as s:
            env = await uc.execute(s, blueprint_name=order_kwargs.pop("blueprint_name"),
                                   ttl_minutes=_TTL, user_id=user_id, **order_kwargs)
    return env, seen


def _provision(Session, vm_id, ids):
    with patch("app.tasks.provision.SyncSessionLocal", Session), \
         patch("app.tasks.provision.asyncio.run", return_value={"ip": "192.168.100.5"}):
        from app.tasks.provision import provision_vm_task
        provision_vm_task.run(str(vm_id), str(ids["image"]), str(ids["hw"]))


def _assert_single_lease_started_after(Session, env_id, earliest):
    env_expiry, child_expiries = expiries(Session, env_id)
    assert env_expiry != PERMANENT_EXPIRES_AT
    assert env_expiry >= earliest + timedelta(minutes=_TTL)  # not stamped mid-order
    assert all(e == env_expiry for e in child_expiries)


async def test_reconciliation_mid_order_does_not_stamp_pooled_child(async_engine: AsyncEngine, seed_user):
    """(a) The READY namespace child is committed; the VM child doesn't exist yet."""
    ids = await _seed_catalog(async_engine, f"race-a-{uuid4().hex[:6]}")
    Session = make_sessionmaker()
    env = None
    try:
        started = datetime.now(timezone.utc)
        env, seen = await _order_pausing_after_first_child(
            async_engine, Session, ids, str(seed_user), blueprint_name=await _bp_name(async_engine, ids),
        )
        assert seen["first_child_status"] == BookingStatus.READY
        assert seen["mid_order_expiry"] == PERMANENT_EXPIRES_AT
        assert expiries(Session, env.id)[0] == PERMANENT_EXPIRES_AT  # VM still PENDING

        vm = next(b for b in env.bookings if b.resource_type == ResourceType.VM)
        _provision(Session, vm.id, ids)
        _assert_single_lease_started_after(Session, env.id, started)
    finally:
        if env is not None:
            cleanup(Session, [env.id])
        await _drop_catalog(async_engine, ids, seed_user)


async def test_reconciliation_mid_order_does_not_stamp_adopted_namespace(async_engine: AsyncEngine, seed_user):
    """(b) The user's READY standalone namespace is adopted; the VM child doesn't exist yet."""
    ids = await _seed_catalog(async_engine, f"race-b-{uuid4().hex[:6]}")
    Session = make_sessionmaker()
    standalone_id = uuid4()
    with Session() as s:
        s.add(BookingModel(
            id=standalone_id, user_id=str(seed_user), status="READY", resource_type="NAMESPACE",
            namespace_id=ids["ns"], ttl_minutes=_TTL,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=_TTL),
        ))
        s.commit()
    env = None
    try:
        started = datetime.now(timezone.utc)
        env, seen = await _order_pausing_after_first_child(
            async_engine, Session, ids, str(seed_user),
            blueprint_name=await _bp_name(async_engine, ids), namespace_id=ids["ns"],
        )
        ns_child = next(b for b in env.bookings if b.resource_type == ResourceType.NAMESPACE)
        assert ns_child.id == standalone_id  # adopted, not re-reserved
        assert seen["mid_order_expiry"] == PERMANENT_EXPIRES_AT

        vm = next(b for b in env.bookings if b.resource_type == ResourceType.VM)
        _provision(Session, vm.id, ids)
        _assert_single_lease_started_after(Session, env.id, started)
    finally:
        if env is not None:
            cleanup(Session, [env.id])
        with Session() as s:
            s.execute(delete(BookingAuditModel).where(BookingAuditModel.booking_id == standalone_id))
            s.execute(delete(BookingModel).where(BookingModel.id == standalone_id))
            s.commit()
        await _drop_catalog(async_engine, ids, seed_user)


def test_queued_child_promoted_during_construction_does_not_stamp():
    """(c) Another worker frees a namespace and promotes this environment's QUEUED child to READY
    while the order is still creating the rest — the promotion's lease check must not stamp."""
    Session = make_sessionmaker()
    ns_id = uuid4()
    with Session() as s:
        s.add(NamespaceModel(id=ns_id, name="race-c-ns", cluster_name=f"race-c-{ns_id}",
                             api_url=None, is_active=True))
        s.commit()
    env_id, (child_id,) = insert_environment(
        Session, ["QUEUED"], resource_types=["NAMESPACE"], construction_complete=False,
    )
    try:
        with Session() as s:
            BookingRepository().sync_promote_next_queued(s, ResourceType.NAMESPACE.value)
        with Session() as s:
            assert BookingRepository().sync_get(s, child_id).status == BookingStatus.READY
        assert expiries(Session, env_id)[0] == PERMANENT_EXPIRES_AT
    finally:
        cleanup(Session, [env_id], [ns_id])


async def _bp_name(async_engine, ids):
    async with AsyncSession(async_engine) as s:
        return (await s.get(EnvironmentBlueprintModel, ids["bp"])).name
