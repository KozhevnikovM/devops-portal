"""Real transaction boundaries for reporting, release completion and FIFO (#535).

Commit gates hold actual repository transactions open; PostgreSQL lock observations
prove contenders reached the contested parent before the gate is opened.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.deployment import DeploymentReport, DeploymentStatus
from app.domain.enums import BookingStatus
from app.domain.exceptions import EnvironmentError
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentModel,
    NamespaceModel,
)
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from tests.integration._environment_lease import (
    cleanup,
    insert_environment,
    make_sessionmaker,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres_integration,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest.fixture(autouse=True)
def _no_notification_transport(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.repositories.environment_repo.apublish_environment_changed",
        AsyncMock(),
    )


def _report(ref="main", status=DeploymentStatus.SUCCESS):
    return DeploymentReport(
        deployment_ref=ref,
        deployment_commit=("a" if ref == "main" else "b") * 40,
        deployment_status=status,
        deployment_frontend_url=f"https://example.test/{ref}?mode=1#app",
        deployment_admin_url=f"https://example.test/{ref}/admin",
    )


def _fields(value):
    return tuple(
        getattr(value, name)
        for name in (
            "deployment_ref",
            "deployment_commit",
            "deployment_status",
            "deployment_frontend_url",
            "deployment_admin_url",
        )
    )


async def _write(engine, env_id, report, session=None):
    if session is not None:
        return await EnvironmentRepository().update_deployment(session, env_id, report)
    async with AsyncSession(engine, expire_on_commit=False) as own:
        return await EnvironmentRepository().update_deployment(own, env_id, report)


async def _wait_for_parent_waiter(engine):
    """Wait on database evidence, never choose an interleaving with a timed sleep."""
    async with asyncio.timeout(10):
        async with AsyncSession(engine) as observer:
            while True:
                count = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
                if count:
                    return


def _pause_commit(session):
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = session.commit

    async def gated():
        entered.set()
        await proceed.wait()
        await original()

    session.commit = gated
    return entered, proceed


async def _finish(tasks):
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)


async def test_overlapping_reports_return_their_own_committed_snapshot(async_engine):
    Session = make_sessionmaker()
    env_id, _ = insert_environment(Session, ["READY"])
    committed, return_first = asyncio.Event(), asyncio.Event()
    entered, allow_commit = asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as first:
            original = first.commit

            async def delay_after_commit():
                entered.set()
                await allow_commit.wait()
                await original()
                committed.set()
                await return_first.wait()

            first.commit = delay_after_commit
            tasks.append(
                asyncio.create_task(_write(async_engine, env_id, _report(), first))
            )
            await asyncio.wait_for(entered.wait(), 10)
            tasks.append(
                asyncio.create_task(_write(async_engine, env_id, _report("next")))
            )
            await _wait_for_parent_waiter(async_engine)
            allow_commit.set()
            await asyncio.wait_for(committed.wait(), 10)
            second = await asyncio.wait_for(tasks[1], 10)
            return_first.set()
            accepted_first = await asyncio.wait_for(tasks[0], 10)
        assert _fields(accepted_first) == _fields(_report())
        assert _fields(second) == _fields(_report("next"))
        assert accepted_first.deployment_updated_at is not None
        assert second.deployment_updated_at >= accepted_first.deployment_updated_at
        with Session() as s:
            stored = s.get(EnvironmentModel, env_id)
            assert _fields(stored) == _fields(second)
            assert stored.deployment_updated_at == second.deployment_updated_at
    finally:
        allow_commit.set()
        return_first.set()
        await _finish(tasks)
        cleanup(Session, [env_id])


@pytest.mark.parametrize("release_path", ["manual-async", "worker-sync", "ttl-sync"])
@pytest.mark.parametrize(
    "report_first", [False, True], ids=["release-wins", "report-wins"]
)
async def test_report_and_final_release_share_parent_serialization(
    async_engine,
    release_path,
    report_first,
):
    Session = make_sessionmaker()
    sync_release = release_path != "manual-async"
    ttl_release = release_path == "ttl-sync"
    env_id, children = insert_environment(
        Session,
        ["READY" if ttl_release else "RELEASING"],
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        resource_types=["NAMESPACE" if ttl_release else "VM"],
    )
    tasks = []
    sync_entered, sync_proceed = Event(), Event()
    entered, proceed = asyncio.Event(), asyncio.Event()

    def worker_release():
        with Session() as s:
            original = s.commit

            def gated():
                sync_entered.set()
                if not sync_proceed.wait(10):
                    raise AssertionError("release commit gate was not opened")
                original()

            if not report_first:
                s.commit = gated
            if ttl_release:
                from app.tasks.beat_tasks import _release_child_sync

                expired = EnvironmentRepository().sync_live_children_if_expired(
                    s, env_id
                )
                assert len(expired) == 1
                _release_child_sync(s, expired[0], lambda _: None)
            else:
                BookingRepository().sync_update_status(
                    s, children[0], BookingStatus.RELEASED
                )

    async def release(session):
        if sync_release:
            return await asyncio.to_thread(worker_release)
        await BookingRepository().update_status(
            session, children[0], BookingStatus.RELEASED
        )

    try:
        async with (
            AsyncSession(async_engine, expire_on_commit=False) as reporting,
            AsyncSession(async_engine, expire_on_commit=False) as releasing,
        ):
            gated_session = reporting if report_first else releasing
            entered, proceed = _pause_commit(gated_session)
            first_coro = (
                _write(async_engine, env_id, _report(), reporting)
                if report_first
                else release(releasing)
            )
            tasks.append(asyncio.create_task(first_coro))
            if sync_release and not report_first:
                assert await asyncio.to_thread(sync_entered.wait, 10)
            else:
                await asyncio.wait_for(entered.wait(), 10)
            second_coro = (
                release(releasing)
                if report_first
                else _write(async_engine, env_id, _report(), reporting)
            )
            tasks.append(asyncio.create_task(second_coro))
            await _wait_for_parent_waiter(async_engine)
            proceed.set()
            sync_proceed.set()
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), 10
            )
        report_result = results[0 if report_first else 1]
        release_result = results[1 if report_first else 0]
        assert release_result is None
        if report_first:
            assert _fields(report_result) == _fields(_report())
        else:
            assert isinstance(report_result, EnvironmentError)
        with Session() as s:
            assert s.get(BookingModel, children[0]).status == "RELEASED"
            stored = s.get(EnvironmentModel, env_id)
            assert stored.deployment_ref == ("main" if report_first else None)
    finally:
        proceed.set()
        sync_proceed.set()
        await _finish(tasks)
        cleanup(Session, [env_id])


@pytest.mark.parametrize(
    "with_newer", [False, True], ids=["sole-queued", "oldest-queued"]
)
async def test_report_holding_parent_never_skips_queued_child(async_engine, with_newer):
    Session = make_sessionmaker()
    env_id, queued = insert_environment(
        Session, ["QUEUED"], resource_types=["NAMESPACE"]
    )
    holder_id, held = insert_environment(
        Session, ["READY"], resource_types=["NAMESPACE"]
    )
    env_ids = [env_id, holder_id]
    newer = []
    if with_newer:
        newer_id, newer = insert_environment(
            Session, ["QUEUED"], resource_types=["NAMESPACE"]
        )
        env_ids.append(newer_id)
    ns_id = uuid4()
    with Session() as s:
        s.add(
            NamespaceModel(
                id=ns_id,
                name=f"!deployment-{ns_id}",
                cluster_name="deployment-test",
                is_active=True,
            )
        )
        s.flush()
        s.execute(
            update(BookingModel)
            .where(BookingModel.id == held[0])
            .values(namespace_id=ns_id)
        )
        s.execute(
            update(BookingModel)
            .where(BookingModel.id == queued[0])
            .values(created_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
        s.commit()
    tasks = []
    proceed = asyncio.Event()
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as reporting:
            entered, proceed = _pause_commit(reporting)
            tasks.append(
                asyncio.create_task(_write(async_engine, env_id, _report(), reporting))
            )
            await asyncio.wait_for(entered.wait(), 10)
            async with AsyncSession(async_engine, expire_on_commit=False) as releasing:
                await asyncio.wait_for(
                    BookingRepository().update_status(
                        releasing, held[0], BookingStatus.RELEASED
                    ),
                    10,
                )

            async def promote():
                async with AsyncSession(async_engine, expire_on_commit=False) as s:
                    return await BookingRepository().promote_next_queued(s, "NAMESPACE")

            tasks.append(asyncio.create_task(promote()))
            # Promotion commits booking assignment before trying the parent lease lock.
            # Seeing it wait proves the candidate was selected rather than SKIP LOCKED.
            await _wait_for_parent_waiter(async_engine)
            with Session() as s:
                selected = s.get(BookingModel, queued[0])
                assert (selected.status, selected.namespace_id) == ("READY", ns_id)
                if newer:
                    assert s.get(BookingModel, newer[0]).status == "QUEUED"
                assert (
                    s.scalar(
                        select(func.count())
                        .select_from(BookingAuditModel)
                        .where(
                            BookingAuditModel.booking_id == queued[0],
                            BookingAuditModel.new_status == "READY",
                        )
                    )
                    == 1
                )
            proceed.set()
            report_result, promoted = await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert promoted.id == queued[0]
            assert _fields(report_result) == _fields(_report())
        with Session() as s:
            env = s.get(EnvironmentModel, env_id)
            child = s.get(BookingModel, queued[0])
            assert env.expires_at == child.expires_at
    finally:
        proceed.set()
        await _finish(tasks)
        cleanup(Session, env_ids, [ns_id])


async def test_report_and_lease_extension_both_complete_without_split_deadlines(
    async_engine,
):
    Session = make_sessionmaker()
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    env_id, children = insert_environment(Session, ["READY"], expires_at=deadline)
    with Session() as s:
        s.execute(
            update(BookingModel)
            .where(BookingModel.id.in_(children))
            .values(expires_at=deadline)
        )
        s.commit()
    tasks = []
    proceed = asyncio.Event()
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as reporting:
            entered, proceed = _pause_commit(reporting)
            tasks.append(
                asyncio.create_task(_write(async_engine, env_id, _report(), reporting))
            )
            await asyncio.wait_for(entered.wait(), 10)

            async def extend():
                async with AsyncSession(async_engine, expire_on_commit=False) as s:
                    await EnvironmentRepository().extend_lease(
                        s, env_id, 60, actor_id="owner"
                    )

            tasks.append(asyncio.create_task(extend()))
            await _wait_for_parent_waiter(async_engine)
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
        with Session() as s:
            env = s.get(EnvironmentModel, env_id)
            child = s.get(BookingModel, children[0])
            assert (
                env.expires_at == child.expires_at == deadline + timedelta(minutes=60)
            )
            assert env.ttl_minutes == child.ttl_minutes == 120
            assert env.deployment_ref == "main"
    finally:
        proceed.set()
        await _finish(tasks)
        cleanup(Session, [env_id])


async def test_failed_report_rolls_back_complete_previous_report_and_releases_lock(
    async_engine,
):
    Session = make_sessionmaker()
    env_id, _ = insert_environment(Session, ["READY"])
    try:
        previous = await _write(async_engine, env_id, _report())
        async with AsyncSession(async_engine, expire_on_commit=False) as s:

            async def fail_commit():
                # Flush real changes before simulating the failing commit: rollback
                # must undo them and relinquish the serialization lock.
                await s.flush()
                raise RuntimeError("simulated transaction failure")

            s.commit = fail_commit
            with pytest.raises(RuntimeError, match="simulated transaction failure"):
                await _write(async_engine, env_id, _report("next"), s)
        with Session() as s:
            stored = s.get(EnvironmentModel, env_id)
            assert _fields(stored) == _fields(previous)
            assert stored.deployment_updated_at == previous.deployment_updated_at
        following = await asyncio.wait_for(
            _write(async_engine, env_id, _report("next")), 10
        )
        assert _fields(following) == _fields(_report("next"))
    finally:
        cleanup(Session, [env_id])


async def test_missed_report_event_reconciles_all_fields_with_existing_read_bounds(
    async_engine,
    seed_user,
    monkeypatch,
):
    from dataclasses import replace
    from types import SimpleNamespace
    from uuid import UUID

    from app.domain.entities import User
    from app.main import app
    from tests.integration.test_reconcile_cost import _measure, _query, _versions

    Session = make_sessionmaker()
    env_id, _ = insert_environment(Session, ["READY"])
    owner = User(
        id=UUID(seed_user),
        username="deployment-owner",
        password_hash="",
        role="user",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        app.state,
        "environment_child_limit",
        SimpleNamespace(value=4, request_refresh=lambda: None),
        raising=False,
    )
    with Session() as s:
        s.execute(
            update(EnvironmentModel)
            .where(EnvironmentModel.id == env_id)
            .values(user_id=seed_user)
        )
        s.commit()
    try:
        async with (
            async_engine.connect() as connection,
            AsyncSession(connection, expire_on_commit=False) as s,
        ):
            initial = await _measure(
                s, owner, "/environments/reconcile?" + _query([env_id])
            )
            initial_versions = _versions(initial["html"])
            assert str(env_id) in initial_versions
            accepted = await _write(async_engine, env_id, _report())
            reconciled = await _measure(
                s,
                owner,
                "/environments/reconcile?" + _query([env_id], initial_versions),
            )
            assert reconciled["statements"] <= 5
            assert reconciled["catalog_calls"] == 0
            assert len(reconciled["child_rows"]) == 1
            assert "https://example.test/main?mode=1#app" in reconciled["html"]
            assert "a" * 40 in reconciled["html"]
            assert (
                _versions(reconciled["html"])[str(env_id)]
                != initial_versions[str(env_id)]
            )
            listed = (
                await EnvironmentRepository().list_page(
                    s,
                    user_id=seed_user,
                    label=None,
                    include_released=True,
                    limit=1,
                    after=None,
                )
            ).items[0]
            [batch], over = await EnvironmentRepository().list_items_by_ids(
                s,
                [env_id],
                user_id=seed_user,
                child_limit=4,
            )
            assert over == []
            for projected in (listed, batch):
                assert _fields(projected) == _fields(accepted)
                assert projected.deployment_updated_at == accepted.deployment_updated_at
            assert not hasattr(batch.bookings[0], "vm_password")
            # An exact URL-only change must invalidate the old row version too.
            changed = replace(
                _report(), deployment_frontend_url="https://example.test/new?x=1#url"
            )
            await _write(async_engine, env_id, changed)
            url_only = await _measure(
                s,
                owner,
                "/environments/reconcile?"
                + _query([env_id], _versions(reconciled["html"])),
            )
            assert url_only["statements"] <= 5
            assert "https://example.test/new?x=1#url" in url_only["html"]
            assert (
                _versions(url_only["html"])[str(env_id)]
                != _versions(reconciled["html"])[str(env_id)]
            )
    finally:
        app.dependency_overrides.clear()
        cleanup(Session, [env_id])


@pytest.mark.parametrize(
    "report_first", [True, False], ids=["report-wins", "detach-wins"]
)
async def test_detaching_last_live_child_serializes_with_report(
    async_engine, report_first
):
    from sqlalchemy import delete

    Session = make_sessionmaker()
    env_id, children = insert_environment(Session, ["RELEASED", "READY"])
    tasks = []
    proceed = asyncio.Event()
    try:
        async with (
            AsyncSession(async_engine, expire_on_commit=False) as reporting,
            AsyncSession(async_engine, expire_on_commit=False) as detaching,
        ):
            entered, proceed = _pause_commit(reporting if report_first else detaching)

            async def detach():
                await BookingRepository().set_environment(
                    detaching,
                    children[1],
                    None,
                    None,
                    60,
                    datetime.now(UTC) + timedelta(hours=1),
                )

            tasks.append(
                asyncio.create_task(
                    _write(async_engine, env_id, _report(), reporting)
                    if report_first
                    else detach(),
                )
            )
            await asyncio.wait_for(entered.wait(), 10)
            tasks.append(
                asyncio.create_task(
                    detach()
                    if report_first
                    else _write(async_engine, env_id, _report(), reporting),
                )
            )
            await _wait_for_parent_waiter(async_engine)
            proceed.set()
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), 10
            )
        report_result = results[0 if report_first else 1]
        assert results[1 if report_first else 0] is None
        if report_first:
            assert _fields(report_result) == _fields(_report())
        else:
            assert isinstance(report_result, EnvironmentError)
        with Session() as s:
            assert s.get(BookingModel, children[1]).environment_id is None
            assert s.get(EnvironmentModel, env_id).deployment_ref == (
                "main" if report_first else None
            )
    finally:
        proceed.set()
        await _finish(tasks)
        # The detached child is no longer reached by environment-based cleanup.
        with Session() as s:
            s.execute(
                delete(BookingAuditModel).where(
                    BookingAuditModel.booking_id.in_(children)
                )
            )
            s.execute(delete(BookingModel).where(BookingModel.id.in_(children)))
            s.commit()
        cleanup(Session, [env_id])
