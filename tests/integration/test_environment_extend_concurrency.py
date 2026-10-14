"""Integration: extending an environment's shared lease under concurrency (#532).

Every interleaving is forced deterministically: a sync session holds the contested row lock, the
competing async operations are started and observed *waiting on that lock* (pg_stat_activity),
and only then is the lock released — so the order in which they reach the row is the order the
test chose, not a timing accident.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.enums import BookingStatus
from app.domain.exceptions import EnvironmentChildExtendError, EnvironmentError
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentModel,
)
from app.infrastructure.repositories import environment_repo as env_repo_mod
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

_READY = BookingStatus.READY.value


def _started_env(Session, n_children=3, *, expires_at=None, ttl_minutes=60):
    """A READY, fully constructed environment whose lease has started (env + children share it)."""
    expires_at = expires_at or (datetime.now(UTC) + timedelta(minutes=30)).replace(
        microsecond=0
    )
    env_id, child_ids = insert_environment(
        Session,
        [_READY] * n_children,
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        resource_types=["NAMESPACE", "STATIC_VM", "VM"][:n_children],
    )
    with Session() as s:
        s.execute(
            update(BookingModel)
            .where(BookingModel.environment_id == env_id)
            .values(expires_at=expires_at)
        )
        s.commit()
    return env_id, child_ids, expires_at


def _leases(Session, env_id):
    with Session() as s:
        env = s.get(EnvironmentModel, env_id)
        children = s.execute(
            select(BookingModel.ttl_minutes, BookingModel.expires_at).where(
                BookingModel.environment_id == env_id
            )
        ).all()
        return (env.ttl_minutes, env.expires_at), [tuple(c) for c in children]


def _audit(Session, booking_ids, action="EXTENDED"):
    with Session() as s:
        return (
            s.execute(
                select(BookingAuditModel).where(
                    BookingAuditModel.booking_id.in_(booking_ids),
                    BookingAuditModel.action == action,
                )
            )
            .scalars()
            .all()
        )


async def _wait_for_lock_waiters(Session, n: int, timeout: float = 10.0) -> None:
    """Until at least ``n`` backends of this database are blocked on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session() as s:
            waiting = s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {n} lock waiter(s)")


async def _extend(async_engine, env_id, minutes, actor="actor"):
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await EnvironmentRepository().extend_lease(s, env_id, minutes, actor_id=actor)


async def _outcome(coro):
    try:
        await coro
        return "ok"
    except Exception as exc:  # noqa: BLE001 — the test asserts on the type
        return exc


# ── 2.1 / 6.x: the happy path writes one deadline + audit for the whole stack ───────
async def test_extend_lease_moves_env_and_every_child_together(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    env_id, child_ids, t = _started_env(Session)
    try:
        await _extend(async_engine, env_id, 720, actor="dispatcher-1")

        (env_ttl, env_exp), children = _leases(Session, env_id)
        assert (env_ttl, env_exp) == (780, t + timedelta(minutes=720))
        assert children == [(780, t + timedelta(minutes=720))] * 3
        entries = _audit(Session, child_ids)
        assert len(entries) == 3
        assert {e.actor_id for e in entries} == {"dispatcher-1"}
        assert all(
            e.extra == {"extend_minutes": 720, "environment_id": str(env_id)}
            for e in entries
        )
    finally:
        cleanup(Session, [env_id])


# ── 6.1: two concurrent extensions both apply, cumulatively ─────────────────────────
async def test_two_concurrent_extensions_accumulate(async_engine: AsyncEngine):
    Session = make_sessionmaker()
    env_id, child_ids, t = _started_env(Session)
    try:
        with Session() as holder:
            holder.execute(
                select(EnvironmentModel)
                .where(EnvironmentModel.id == env_id)
                .with_for_update()
            )
            first = asyncio.ensure_future(_outcome(_extend(async_engine, env_id, 60)))
            await _wait_for_lock_waiters(Session, 1)
            second = asyncio.ensure_future(_outcome(_extend(async_engine, env_id, 120)))
            await _wait_for_lock_waiters(Session, 2)
            holder.commit()
        assert await asyncio.gather(first, second) == ["ok", "ok"]

        (env_ttl, env_exp), children = _leases(Session, env_id)
        assert (env_ttl, env_exp) == (240, t + timedelta(minutes=180))
        assert set(children) == {(240, t + timedelta(minutes=180))}
        assert len(_audit(Session, child_ids)) == 6
    finally:
        cleanup(Session, [env_id])


# ── 6.2: an extension committed after the sweep's preselection stops the teardown ───
async def test_sweep_skips_environment_extended_after_preselection(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    past = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
    env_id, child_ids, _ = _started_env(Session, expires_at=past)
    repo = EnvironmentRepository()
    try:
        with Session() as s:
            assert env_id in {e.id for e in repo.sync_list_expired(s)}  # preselected

        await _extend(
            async_engine, env_id, 60
        )  # commits between preselection and release

        with Session() as s:
            assert repo.sync_live_children_if_expired(s, env_id) == []
        with Session() as s:
            statuses = s.execute(
                select(BookingModel.status).where(BookingModel.id.in_(child_ids))
            ).scalars()
            assert set(statuses) == {_READY}
    finally:
        cleanup(Session, [env_id])


# ── 6.3: the sweep wins — a waiting extension is rejected once a child is released ──
async def test_extension_waiting_on_sweep_is_rejected(async_engine: AsyncEngine):
    Session = make_sessionmaker()
    past = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
    env_id, child_ids, _ = _started_env(Session, expires_at=past)
    try:
        before = _leases(Session, env_id)
        with Session() as sweep:
            children = EnvironmentRepository().sync_live_children_if_expired(
                sweep, env_id
            )
            assert len(children) == 3  # env row now locked by the sweep
            extension = asyncio.ensure_future(
                _outcome(_extend(async_engine, env_id, 60))
            )
            await _wait_for_lock_waiters(Session, 1)
            # The sweep's first child release commits (and releases the env lock).
            BookingRepository().sync_update_status(
                sweep, children[0].id, BookingStatus.RELEASING
            )
        result = await extension

        assert isinstance(result, EnvironmentError)
        assert _leases(Session, env_id) == before
        assert _audit(Session, child_ids) == []
    finally:
        cleanup(Session, [env_id])


# ── 6.3: a release racing an extension never leaves the deadlines split ─────────────
async def test_release_committing_while_extension_waits_rejects_it(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    env_id, child_ids, _ = _started_env(Session)
    try:
        before = _leases(Session, env_id)
        with Session() as release:
            # A release has updated one child but not committed yet: it holds that child's row lock.
            release.execute(
                update(BookingModel)
                .where(BookingModel.id == child_ids[0])
                .values(status=BookingStatus.RELEASING.value)
            )
            extension = asyncio.ensure_future(
                _outcome(_extend(async_engine, env_id, 60))
            )
            await _wait_for_lock_waiters(
                Session, 1
            )  # extension passed its pre-check, waits on the child
            release.commit()
        result = await extension

        assert isinstance(result, EnvironmentError)
        assert _leases(Session, env_id) == before
    finally:
        cleanup(Session, [env_id])


async def test_extension_committing_before_release_extends_whole_stack(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    env_id, child_ids, t = _started_env(Session)
    try:
        await _extend(async_engine, env_id, 60)
        with Session() as release:
            BookingRepository().sync_update_status(
                release, child_ids[0], BookingStatus.RELEASING
            )

        (env_ttl, env_exp), children = _leases(Session, env_id)
        assert (
            set(children) == {(env_ttl, env_exp)} == {(120, t + timedelta(minutes=60))}
        )
        # A further extension is now rejected: the stack is being released.
        assert isinstance(
            await _outcome(_extend(async_engine, env_id, 60)), EnvironmentError
        )
    finally:
        cleanup(Session, [env_id])


# ── 6.4: a failure part-way rolls the whole extension back ──────────────────────────
async def test_failure_midway_rolls_back_everything(
    async_engine: AsyncEngine, monkeypatch
):
    Session = make_sessionmaker()
    env_id, child_ids, t = _started_env(Session)
    try:
        before = _leases(Session, env_id)
        real_audit = env_repo_mod.BookingAuditModel
        calls = {"n": 0}

        def failing_audit(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("audit insert failed")
            return real_audit(**kw)

        monkeypatch.setattr(env_repo_mod, "BookingAuditModel", failing_audit)
        result = await _outcome(_extend(async_engine, env_id, 60))
        monkeypatch.setattr(env_repo_mod, "BookingAuditModel", real_audit)

        assert isinstance(result, RuntimeError)
        assert _leases(Session, env_id) == before
        assert _audit(Session, child_ids) == []
        # No lock was left behind: the next extension goes through.
        await _extend(async_engine, env_id, 60)
        assert _leases(Session, env_id)[0] == (120, t + timedelta(minutes=60))
    finally:
        cleanup(Session, [env_id])


# ── 7.3: adoption of a standalone namespace booking vs its standalone extension ─────
def _standalone_ready_booking(Session):
    booking_id = uuid4()
    expires = (datetime.now(UTC) + timedelta(minutes=30)).replace(microsecond=0)
    with Session() as s:
        s.add(
            BookingModel(
                id=booking_id,
                user_id="inttest-owner",
                status=_READY,
                resource_type="NAMESPACE",
                ttl_minutes=60,
                expires_at=expires,
                created_at=datetime.now(UTC),
            )
        )
        s.commit()
    return booking_id, expires


def _delete_booking(Session, booking_id):
    with Session() as s:
        s.execute(
            delete(BookingAuditModel).where(BookingAuditModel.booking_id == booking_id)
        )
        s.execute(delete(BookingModel).where(BookingModel.id == booking_id))
        s.commit()


async def _adopt(async_engine, booking_id, env_id, env_ttl, env_expires):
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await BookingRepository().set_environment(
            s, booking_id, env_id, "ns", env_ttl, env_expires
        )


async def _extend_booking(async_engine, booking_id, minutes):
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        await BookingRepository().extend(s, booking_id, minutes, actor_id="owner")


async def _race(async_engine, Session, booking_id, first, second):
    with Session() as holder:
        holder.execute(
            select(BookingModel).where(BookingModel.id == booking_id).with_for_update()
        )
        a = asyncio.ensure_future(_outcome(first))
        await _wait_for_lock_waiters(Session, 1)
        b = asyncio.ensure_future(_outcome(second))
        await _wait_for_lock_waiters(Session, 2)
        holder.commit()
    return await asyncio.gather(a, b)


async def test_adoption_first_rejects_the_standalone_extension(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    env_id, _ = insert_environment(
        Session, [], ttl_minutes=240
    )  # placeholder (pending) lease
    booking_id, _ = _standalone_ready_booking(Session)
    try:
        with Session() as s:
            env = s.get(EnvironmentModel, env_id)
            env_ttl, env_expires = env.ttl_minutes, env.expires_at
        adopted, extended = await _race(
            async_engine,
            Session,
            booking_id,
            _adopt(async_engine, booking_id, env_id, env_ttl, env_expires),
            _extend_booking(async_engine, booking_id, 720),
        )

        assert adopted == "ok"
        assert isinstance(extended, EnvironmentChildExtendError)
        with Session() as s:
            b = s.get(BookingModel, booking_id)
            assert (b.environment_id, b.ttl_minutes, b.expires_at) == (
                env_id,
                env_ttl,
                env_expires,
            )
        assert _audit(Session, [booking_id]) == []
    finally:
        _delete_booking(Session, booking_id)
        cleanup(Session, [env_id])


async def test_extension_first_then_adoption_takes_environment_lease(
    async_engine: AsyncEngine,
):
    Session = make_sessionmaker()
    env_id, _ = insert_environment(Session, [], ttl_minutes=240)
    booking_id, _ = _standalone_ready_booking(Session)
    try:
        with Session() as s:
            env = s.get(EnvironmentModel, env_id)
            env_ttl, env_expires = env.ttl_minutes, env.expires_at
        extended, adopted = await _race(
            async_engine,
            Session,
            booking_id,
            _extend_booking(async_engine, booking_id, 720),
            _adopt(async_engine, booking_id, env_id, env_ttl, env_expires),
        )

        assert (extended, adopted) == ("ok", "ok")
        with Session() as s:
            b = s.get(BookingModel, booking_id)
            assert (b.environment_id, b.ttl_minutes, b.expires_at) == (
                env_id,
                env_ttl,
                env_expires,
            )
        assert len(_audit(Session, [booking_id])) == 1  # the standalone extension
        assert len(_audit(Session, [booking_id], "ADOPTED")) == 1  # then the adoption
    finally:
        _delete_booking(Session, booking_id)
        cleanup(Session, [env_id])
