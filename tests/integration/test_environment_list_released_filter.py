"""Integration: the environments list hides fully released environments in SQL (#466).

An environment is fully released only when it has at least one child and every child is RELEASED —
the same rule `derive_environment_status` applies in Python. With `include_released=False` those
environments are excluded by the list query itself, so their children are never loaded.
"""
from datetime import datetime, timedelta, timezone
from itertools import combinations_with_replacement
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import User
from app.domain.enums import BookingStatus
from app.domain.environment_status import derive_environment_status
from app.infrastructure.database.models import BookingModel, EnvironmentModel
from app.infrastructure.repositories.environment_repo import EnvironmentRepository, _list_stmt

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_repo = EnvironmentRepository()


async def _seed(
    session: AsyncSession, owner: str, envs: list[list[BookingStatus]], *, name: str = "env",
) -> list[UUID]:
    """Insert one environment per entry, with one child per listed status; return the env ids."""
    base = datetime.now(timezone.utc)
    env_ids: list[UUID] = []
    env_rows, child_rows = [], []
    for i, statuses in enumerate(envs):
        env_id = uuid4()
        env_ids.append(env_id)
        env_rows.append({
            "id": env_id, "name": f"{name}-{i}", "user_id": owner, "ttl_minutes": 60,
            "expires_at": PERMANENT_EXPIRES_AT, "construction_complete": True,
            # Distinct created_at keeps the list ordering deterministic.
            "created_at": base - timedelta(seconds=i),
        })
        child_rows += [{
            "id": uuid4(), "user_id": owner, "status": s.value, "resource_type": "VM",
            "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT, "environment_id": env_id,
        } for s in statuses]
    await session.execute(insert(EnvironmentModel), env_rows)
    if child_rows:
        await session.execute(insert(BookingModel), child_rows)
    await session.flush()
    return env_ids


async def _ids(session, owner, *, include_released, label=None) -> set[UUID]:
    envs = await _repo.list_by_user(session, owner, label=label, include_released=include_released)
    return {e.id for e in envs}


# ── 4.2: behaviour per scenario ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("statuses, visible", [
    pytest.param([S.RELEASED, S.RELEASED], False, id="fully-released"),
    pytest.param([S.RELEASED, S.READY], True, id="released+ready"),
    pytest.param([S.RELEASED, S.RELEASING], True, id="released+releasing"),
    pytest.param([S.FAILED, S.RELEASED], True, id="failed+released"),
    pytest.param([S.READY, S.READY], True, id="all-ready"),
    pytest.param([S.READY, S.PROVISIONING], True, id="in-flight"),
    pytest.param([S.QUEUED], True, id="queued"),
    pytest.param([], True, id="zero-children"),
])
async def test_released_filter_per_scenario(async_session, statuses, visible):
    owner = f"inttest-{uuid4()}"
    [env_id] = await _seed(async_session, owner, [statuses])

    assert (env_id in await _ids(async_session, owner, include_released=False)) is visible
    # show_released lists every environment, whatever its children.
    assert env_id in await _ids(async_session, owner, include_released=True)


async def test_zero_children_environment_is_listed_as_ready(async_session):
    owner = f"inttest-{uuid4()}"
    [env_id] = await _seed(async_session, owner, [[]])

    [env] = await _repo.list_by_user(async_session, owner, include_released=False)
    assert env.id == env_id
    assert derive_environment_status(b.status for b in env.bookings) == S.READY


async def test_fully_released_environment_is_shown_as_released_on_request(async_session):
    owner = f"inttest-{uuid4()}"
    [env_id] = await _seed(async_session, owner, [[S.RELEASED, S.RELEASED]])

    [env] = await _repo.list_by_user(async_session, owner, include_released=True)
    assert env.id == env_id
    assert derive_environment_status(b.status for b in env.bookings) == S.RELEASED


async def test_label_and_owner_filters_combine_with_released_filter(async_session):
    owner, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    token = f"tok{uuid4().hex[:8]}"
    active_match, released_match = await _seed(
        async_session, owner, [[S.READY], [S.RELEASED]], name=f"{token}-mine",
    )
    [active_nomatch] = await _seed(async_session, owner, [[S.READY]], name="unrelated")
    [other_active_match] = await _seed(async_session, other, [[S.READY]], name=f"{token}-other")

    assert await _ids(async_session, owner, include_released=False, label=token) == {active_match}
    assert await _ids(async_session, owner, include_released=False) == {active_match, active_nomatch}

    all_envs = await _repo.list_all(async_session, label=token, include_released=False)
    assert {e.id for e in all_envs} == {active_match, other_active_match}
    all_envs = await _repo.list_all(async_session, label=token, include_released=True)
    assert {e.id for e in all_envs} == {active_match, released_match, other_active_match}


# ── 4.3: the SQL filter and the domain rule agree ────────────────────────────────────────────

def _status_combinations() -> list[list[BookingStatus]]:
    combos: list[list[BookingStatus]] = [[]]
    combos += [[s] for s in S]                                          # every single status
    combos += [list(c) for c in combinations_with_replacement(S, 2)]    # every pair
    combos += [[S.RELEASED] * 3, [S.RELEASED, S.RELEASED, S.READY], [S.RELEASED, S.RELEASED, S.FAILED]]
    return combos


async def test_sql_filter_matches_derive_environment_status(async_session):
    owner = f"inttest-{uuid4()}"
    combos = _status_combinations()
    env_ids = await _seed(async_session, owner, combos)

    expected = {
        env_id for env_id, statuses in zip(env_ids, combos)
        if derive_environment_status(statuses) != S.RELEASED
    }
    assert await _ids(async_session, owner, include_released=False) == expected
    assert await _ids(async_session, owner, include_released=True) == set(env_ids)


async def test_json_list_still_includes_fully_released_environments(async_session):
    from app.presentation.routes.api_environments import list_environments

    user_id = uuid4()
    [env_id] = await _seed(async_session, str(user_id), [[S.RELEASED, S.RELEASED]])
    user = User(
        id=user_id, username="inttest", password_hash="unused", role="user", is_active=True,
        created_at=datetime.now(timezone.utc),
    )

    body = await list_environments(label=None, session=async_session, current_user=user)

    [row] = [r for r in body if r["id"] == str(env_id)]
    assert row["status"] == S.RELEASED.value


# ── 4.4 / 4.5: large released history, small active set ──────────────────────────────────────

_RELEASED_HISTORY = 500


async def _seed_history(session) -> tuple[str, set[UUID]]:
    owner = f"inttest-{uuid4()}"
    released = [[S.RELEASED] * 3] * _RELEASED_HISTORY
    active = [[S.READY, S.READY], [S.PROVISIONING, S.RELEASED], [S.FAILED]]
    env_ids = await _seed(session, owner, active + released)
    return owner, set(env_ids[:len(active)])


async def test_released_history_children_are_never_loaded(async_session, monkeypatch):
    owner, active_ids = await _seed_history(async_session)
    loaded: list[list[UUID]] = []
    original = _repo._children_batch

    async def spy(session, env_ids):
        loaded.append(list(env_ids))
        return await original(session, env_ids)

    monkeypatch.setattr(_repo, "_children_batch", spy)
    envs = await _repo.list_by_user(async_session, owner, include_released=False)

    assert {e.id for e in envs} == active_ids
    assert [set(ids) for ids in loaded] == [active_ids]
    assert {b.environment_id for e in envs for b in e.bookings} == active_ids


async def test_child_probes_can_use_environment_id_indexes(async_session):
    """Both child probes of the hidden-released query must be answerable from an index.

    Whether the planner *prefers* the index depends on table size and visibility (the PR records
    cost-based EXPLAIN ANALYZE evidence); what can regress silently is the index becoming unusable,
    e.g. a partial-index predicate the query no longer implies. So seq scans are disabled here and
    the plan must name both indexes.
    """
    owner, _ = await _seed_history(async_session)
    await async_session.execute(text("ANALYZE bookings"))
    await async_session.execute(text("ANALYZE environments"))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await _explain(async_session, _list_stmt(owner, label=None, include_released=False))
    assert "ix_bookings_environment_id " in plan, plan              # "has any child" probe
    assert "ix_bookings_environment_id_unreleased" in plan, plan    # "has a non-RELEASED child" probe
    assert "Seq Scan on bookings" not in plan, plan

    # At realistic sizes the planner evaluates the non-RELEASED probe once, as a hashed subplan
    # over `bookings WHERE status <> 'RELEASED'` with no environment_id condition — so the partial
    # index must be usable for that bare predicate (an extra `environment_id IS NOT NULL` in the
    # index predicate silently broke this; the correlated form above can't catch it).
    hashed_probe = select(BookingModel.environment_id).where(
        BookingModel.status != BookingStatus.RELEASED.value
    )
    plan = await _explain(async_session, hashed_probe)
    assert "ix_bookings_environment_id_unreleased" in plan, plan


async def _explain(session, stmt) -> str:
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return "\n".join((await session.execute(text(f"EXPLAIN {sql}"))).scalars())
