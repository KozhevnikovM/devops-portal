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
