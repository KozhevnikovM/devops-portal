"""Integration: lease reconciliation is the crash-safe guarantee, and it is retroactive (#434).

Every immediate lease trigger runs in its own transaction *after* the settling commit it follows,
so a process dying in between would leave the environment on the placeholder expiry forever.
`reconcile_environment_leases` finds such environments and starts their lease.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.database.models import NamespaceModel
from app.infrastructure.repositories import booking_repo as booking_repo_mod
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from tests.integration._environment_lease import (
    cleanup,
    expiries,
    insert_environment,
    make_sessionmaker,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration]


class _Crash(BaseException):
    """Simulated process death: a BaseException, so no `except Exception` handler swallows it."""


@pytest.fixture
def Session():
    return make_sessionmaker()


def _reconcile(Session):
    from app.tasks.beat_tasks import reconcile_environment_leases
    with patch("app.tasks.beat_tasks.SyncSessionLocal", Session):
        reconcile_environment_leases()


def _assert_lease_started(Session, env_id, ttl_minutes):
    env_expiry, child_expiries = expiries(Session, env_id)
    now = datetime.now(timezone.utc)
    # Full TTL from *now* (the reconciliation run), not backdated.
    assert now + timedelta(minutes=ttl_minutes - 1) < env_expiry <= now + timedelta(minutes=ttl_minutes)
    assert all(e == env_expiry for e in child_expiries)


# ── 4.7 candidate query ───────────────────────────────────────────────────────────────────────
def test_list_lease_pending_filters(Session):
    started = datetime.now(timezone.utc) + timedelta(minutes=30)
    envs = {
        "in_flight": insert_environment(Session, ["READY", "PROVISIONING"]),
        "queued": insert_environment(Session, ["READY", "QUEUED"]),
        "already_started": insert_environment(Session, ["READY", "FAILED"], expires_at=started),
        "ttl_zero": insert_environment(Session, ["READY", "FAILED"], ttl_minutes=0),
        "all_failed": insert_environment(Session, ["FAILED", "FAILED"]),
        "released_failed": insert_environment(Session, ["RELEASED", "FAILED"]),
        "ready_failed": insert_environment(Session, ["READY", "FAILED"]),
        "ready_releasing": insert_environment(Session, ["READY", "RELEASING"]),
        "ready_released": insert_environment(Session, ["READY", "RELEASED"]),
        "under_construction": insert_environment(Session, ["READY"], construction_complete=False),
    }
    try:
        with Session() as s:
            pending = set(EnvironmentRepository().sync_list_lease_pending(s))
        mine = {name for name, (env_id, _) in envs.items() if env_id in pending}
        assert mine == {"ready_failed", "ready_releasing", "ready_released"}
    finally:
        cleanup(Session, [env_id for env_id, _ in envs.values()])


# ── 4.8 crash gaps ────────────────────────────────────────────────────────────────────────────
def test_crash_after_queued_child_promotion_is_repaired(Session):
    """(a) The last in-flight child is a QUEUED namespace; promotion commits, then the process dies."""
    ns_id = uuid4()
    with Session() as s:
        s.add(NamespaceModel(id=ns_id, name="inttest-434-ns", cluster_name=f"inttest-434-{ns_id}",
                             api_url=None, is_active=True, created_at=datetime.now(timezone.utc)))
        s.commit()
    env_id, (_vm_id, ns_child_id) = insert_environment(
        Session, ["READY", "QUEUED"], ttl_minutes=90, resource_types=["VM", "NAMESPACE"],
    )
    crashing_env_repo = MagicMock()
    crashing_env_repo.sync_start_lease_if_ready.side_effect = _Crash
    try:
        with Session() as s, \
             patch.object(booking_repo_mod, "_environment_repo", return_value=crashing_env_repo), \
             pytest.raises(_Crash):
            BookingRepository().sync_promote_next_queued(s, ResourceType.NAMESPACE.value)
        with Session() as s:
            assert BookingRepository().sync_get(s, ns_child_id).status == BookingStatus.READY  # committed
        assert expiries(Session, env_id)[0] == PERMANENT_EXPIRES_AT  # the gap

        _reconcile(Session)
        _assert_lease_started(Session, env_id, 90)
    finally:
        cleanup(Session, [env_id], [ns_id])


def test_crash_after_vm_child_ready_is_repaired(Session):
    """(b) The provision task commits the last VM child READY, then dies before its lease check."""
    env_id, (_ns_child_id, vm_id) = insert_environment(
        Session, ["READY", "PROVISIONING"], ttl_minutes=45, resource_types=["NAMESPACE", "VM"],
    )
    image = MagicMock(vapp_template_id="tpl")
    hw = MagicMock(cpus=1, memory_mb=1024, disk_mb=10240)
    try:
        with (
            patch("app.tasks.provision.SyncSessionLocal", Session),
            patch("app.tasks.provision.image_repo") as image_repo,
            patch("app.tasks.provision.hw_config_repo") as hw_repo,
            patch("app.tasks.provision.asyncio.run", return_value={"ip": "192.168.100.7"}),
            patch("app.tasks.provision.env_repo") as env_repo,
        ):
            image_repo.sync_get.return_value = image
            hw_repo.sync_get.return_value = hw
            env_repo.sync_start_lease_if_ready_for_booking.side_effect = _Crash
            from app.tasks.provision import provision_vm_task
            with pytest.raises(_Crash):
                provision_vm_task.run(str(vm_id), str(uuid4()), str(uuid4()))
        with Session() as s:
            assert BookingRepository().sync_get(s, vm_id).status == BookingStatus.READY  # committed
        assert expiries(Session, env_id)[0] == PERMANENT_EXPIRES_AT  # the gap

        _reconcile(Session)
        _assert_lease_started(Session, env_id, 45)
    finally:
        cleanup(Session, [env_id])


def test_second_reconciliation_does_not_move_the_deadline(Session):
    """(c) Reconciliation is idempotent once the lease has started."""
    env_id, _ = insert_environment(Session, ["READY", "FAILED"])
    try:
        _reconcile(Session)
        first, _ = expiries(Session, env_id)
        _reconcile(Session)
        assert expiries(Session, env_id)[0] == first
    finally:
        cleanup(Session, [env_id])


def test_pre_deploy_orphan_is_repaired_retroactively(Session):
    """(d) An environment already orphaned before #434 shipped (READY + RELEASED on the placeholder)
    gets a full TTL from the first reconciliation run; one with nothing live is left alone."""
    orphan_id, _ = insert_environment(Session, ["READY", "RELEASED"], ttl_minutes=60,
                                      resource_types=["VM", "NAMESPACE"])
    dead_id, _ = insert_environment(Session, ["RELEASED", "FAILED"], ttl_minutes=60)
    try:
        _reconcile(Session)
        _assert_lease_started(Session, orphan_id, 60)
        assert expiries(Session, dead_id)[0] == PERMANENT_EXPIRES_AT
    finally:
        cleanup(Session, [orphan_id, dead_id])
