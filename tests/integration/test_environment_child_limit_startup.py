"""Integration: the query behind the effective environment child limit (#497).

`C_eff = max(ENVIRONMENT_MAX_CHILDREN, L)`, where L is the most children of any environment that is
not fully released. Fully released environments never change again, so they don't count.
"""
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import BookingModel, EnvironmentModel
from app.infrastructure.repositories.environment_repo import EnvironmentRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_repo = EnvironmentRepository()


async def _seed_env(session, statuses):
    owner = f"inttest-{uuid4()}"
    env_id = uuid4()
    await session.execute(insert(EnvironmentModel), [{
        "id": env_id, "name": "big", "user_id": owner, "ttl_minutes": 60,
        "expires_at": PERMANENT_EXPIRES_AT, "construction_complete": True,
    }])
    await session.execute(insert(BookingModel), [{
        "id": uuid4(), "user_id": owner, "status": s.value, "resource_type": "NAMESPACE",
        "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT, "environment_id": env_id,
    } for s in statuses])
    await session.flush()


async def _stats(session, limit):
    return await _repo.live_children_over(session, limit)


async def test_live_environment_over_the_limit_is_counted(async_session):
    # Whatever the database already holds, nothing exceeds its own current maximum.
    limit, _ = await _stats(async_session, 0)
    await _seed_env(async_session, [BookingStatus.READY] * (limit + 7))
    assert await _stats(async_session, limit) == (limit + 7, 1)


async def test_partly_released_environment_counts_every_child(async_session):
    limit, _ = await _stats(async_session, 0)
    await _seed_env(async_session, [BookingStatus.RELEASED] * (limit + 6) + [BookingStatus.READY])
    assert await _stats(async_session, limit) == (limit + 7, 1)


async def test_fully_released_environment_does_not_count(async_session):
    limit, _ = await _stats(async_session, 0)
    await _seed_env(async_session, [BookingStatus.RELEASED] * (limit + 7))
    assert await _stats(async_session, limit) == (limit, 0)


# ── plan: the periodic query reads live children only, through the partial index ──────────────
def test_not_released_is_a_literal_not_a_bound_parameter():
    from sqlalchemy.dialects import postgresql

    from app.infrastructure.repositories.environment_repo import _live_children_stmt
    compiled = _live_children_stmt(25).compile(dialect=postgresql.dialect())
    assert "'RELEASED'" in str(compiled)
    assert "RELEASED" not in {str(v) for v in compiled.params.values()}


async def test_generic_plan_uses_the_unreleased_partial_index(async_session):
    """As a prepared statement under a forced generic plan (parameter values unknown when planned),
    under the same ordered-walk pin the repository applies, the live-children scan is the partial
    index and no step scans the bookings table."""
    from sqlalchemy import text

    from app.infrastructure.repositories._ordered_walk import _OrderedWalk
    from app.infrastructure.repositories.environment_repo import _live_children_stmt

    await _seed_env(async_session, [BookingStatus.READY, BookingStatus.RELEASED])
    await async_session.execute(text("ANALYZE bookings"))
    conn = await async_session.connection()
    compiled = _live_children_stmt(25).compile(dialect=conn.dialect)    # asyncpg: $1, $2 …
    values = ", ".join(f"'{v}'" if isinstance(v, str) else str(v)
                       for v in (compiled.params[name] for name in compiled.positiontup))
    # A prepared statement planned generically: parameter values unknown at planning time.
    await conn.exec_driver_sql(f"PREPARE live_children AS {compiled}")
    await async_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    try:
        async with _OrderedWalk(async_session):
            result = await conn.exec_driver_sql(f"EXPLAIN EXECUTE live_children({values})")
            plan = "\n".join(row[0] for row in result)
    finally:
        await conn.exec_driver_sql("DEALLOCATE live_children")
    assert "ix_bookings_environment_id_unreleased" in plan, plan
    assert "Seq Scan on bookings" not in plan, plan
