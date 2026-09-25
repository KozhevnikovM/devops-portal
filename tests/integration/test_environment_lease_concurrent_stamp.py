"""Integration: the environment lease is started exactly once under concurrent settling (#434).

Two children of one environment settle at the same moment, so two workers run the lease check
concurrently. The SELECT … FOR UPDATE on the environment row serialises them: the first stamps, the
second — once it gets the lock — sees the lease already started and does nothing. Without the lock
both read the placeholder and both stamp (each returns True), which these tests catch.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.infrastructure.repositories import environment_repo as env_repo_mod
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from tests.integration._environment_lease import (
    cleanup,
    expiries,
    insert_environment,
    make_sessionmaker,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]

_SETTLED = ["READY", "READY", "FAILED"]


def _assert_one_shared_deadline(Session, env_id, results):
    assert sorted(results) == [False, True], f"lease started {results.count(True)} times"
    env_expiry, child_expiries = expiries(Session, env_id)
    assert env_expiry != PERMANENT_EXPIRES_AT
    assert all(e == env_expiry for e in child_expiries)


def _slow_stamp():
    """Widen the read→stamp window so an unlocked race would reliably double-stamp."""
    original = env_repo_mod._stamp_lease

    def slow(env, children):
        time.sleep(0.3)
        return original(env, children)
    return slow


async def test_async_paths_race_start_lease_once(async_engine: AsyncEngine):
    Session = make_sessionmaker()
    env_id, (child_a, child_b, _) = insert_environment(Session, _SETTLED)
    repo = EnvironmentRepository()
    try:
        async def check(child_id):
            async with AsyncSession(async_engine, expire_on_commit=False) as s:
                return await repo.start_lease_if_ready_for_booking(s, child_id)

        results = await asyncio.gather(check(child_a), check(child_b))
        _assert_one_shared_deadline(Session, env_id, list(results))
    finally:
        cleanup(Session, [env_id])


def test_sync_paths_race_start_lease_once(monkeypatch):
    Session = make_sessionmaker()
    env_id, (child_a, child_b, _) = insert_environment(Session, _SETTLED)
    repo = EnvironmentRepository()
    monkeypatch.setattr(env_repo_mod, "_stamp_lease", _slow_stamp())
    barrier = threading.Barrier(2)
    try:
        def check(child_id):
            with Session() as s:
                barrier.wait()
                return repo.sync_start_lease_if_ready_for_booking(s, child_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(check, [child_a, child_b]))
        _assert_one_shared_deadline(Session, env_id, results)
    finally:
        cleanup(Session, [env_id])


def test_sync_and_async_paths_race_start_lease_once(async_engine: AsyncEngine, monkeypatch):
    """A Celery worker (sync) and a request (async, e.g. promotion) settle children concurrently."""
    Session = make_sessionmaker()
    env_id, (child_a, child_b, _) = insert_environment(Session, _SETTLED)
    repo = EnvironmentRepository()
    monkeypatch.setattr(env_repo_mod, "_stamp_lease", _slow_stamp())
    barrier = threading.Barrier(2)
    async_url = async_engine.url.render_as_string(hide_password=False)
    try:
        def sync_check():
            with Session() as s:
                barrier.wait()
                return repo.sync_start_lease_if_ready_for_booking(s, child_a)

        def async_check():
            async def run():
                from sqlalchemy.ext.asyncio import create_async_engine
                engine = create_async_engine(async_url)
                try:
                    async with AsyncSession(engine, expire_on_commit=False) as s:
                        barrier.wait()
                        return await repo.start_lease_if_ready_for_booking(s, child_b)
                finally:
                    await engine.dispose()
            return asyncio.run(run())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(sync_check), pool.submit(async_check)]
            results = [f.result() for f in futures]
        _assert_one_shared_deadline(Session, env_id, results)
    finally:
        cleanup(Session, [env_id])
