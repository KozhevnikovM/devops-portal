"""Membership changes preserve report/release parent-first ordering (#535)."""

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.domain.exceptions import BookingError
from app.infrastructure.database.models import EnvironmentModel
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _async_release_target,
)


def _result(model):
    result = MagicMock()
    result.scalar_one_or_none.return_value = model
    return result


def _session(models):
    session = MagicMock(spec=AsyncSession)
    session.new = set()
    session.dirty = set()
    session.deleted = set()
    session.no_autoflush = nullcontext()
    session.execute = AsyncMock(side_effect=models)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _read_order(session):
    order = []
    for call in session.execute.call_args_list:
        stmt = call.args[0]
        table = stmt.get_final_froms()[0].name
        locked = stmt._for_update_arg is not None
        parameters = stmt.compile().params
        order.append((table, locked, next(iter(parameters.values()))))
    return order


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, UUID(int=1), UUID(int=3)])
async def test_membership_locks_all_affected_parents_before_child_and_commits(target):
    current = UUID(int=2)
    booking_id = uuid4()
    child = SimpleNamespace(id=booking_id, environment_id=current)
    parents = sorted({current, target} - {None}, key=str)
    session = _session(
        [_result(child), *[_result(None) for _ in parents], _result(child)]
    )
    deadline = datetime.now(UTC)
    await BookingRepository().set_environment(
        session, booking_id, target, "ns", 120, deadline
    )
    assert _read_order(session) == [
        ("bookings", False, booking_id),
        *[("environments", True, parent) for parent in parents],
        ("bookings", True, booking_id),
    ]
    assert child.environment_id == target
    assert child.ttl_minutes == 120
    assert child.expires_at == deadline
    audit = session.add.call_args.args[0]
    assert audit.action == ("ADOPTED" if target is not None else "DETACHED")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_adoption_locks_target_parent_before_booking():
    booking_id, target = uuid4(), uuid4()
    child = SimpleNamespace(id=booking_id, environment_id=None)
    session = _session([_result(child), _result(None), _result(child)])
    await _async_release_target(session, booking_id, target)
    assert _read_order(session) == [
        ("bookings", False, booking_id),
        ("environments", True, target),
        ("bookings", True, booking_id),
    ]


@pytest.mark.asyncio
async def test_changed_membership_drops_locks_before_retrying_new_parent():
    booking_id, old, adopted = uuid4(), UUID(int=1), UUID(int=2)
    before = SimpleNamespace(id=booking_id, environment_id=old)
    after = SimpleNamespace(id=booking_id, environment_id=adopted)
    session = _session(
        [
            _result(before),
            _result(None),
            _result(after),
            _result(after),
            _result(None),
            _result(after),
        ]
    )
    rolled_back_before_retry = []

    async def execute(stmt):
        if len(session.execute.call_args_list) == 4:
            rolled_back_before_retry.append(session.rollback.await_count == 1)
        return results.pop(0)

    results = list(session.execute.side_effect)
    session.execute.side_effect = execute
    returned = await _async_release_target(session, booking_id)
    assert returned is after
    assert rolled_back_before_retry == [True]
    assert _read_order(session) == [
        ("bookings", False, booking_id),
        ("environments", True, old),
        ("bookings", True, booking_id),
        ("bookings", False, booking_id),
        ("environments", True, adopted),
        ("bookings", True, booking_id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
async def test_real_session_rejects_pending_mutation_before_acquiring_child_or_parent(
    pending,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(EnvironmentModel.__table__.create)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            now = datetime.now(UTC)
            env = EnvironmentModel(
                id=uuid4(),
                name="stack",
                user_id="owner",
                ttl_minutes=60,
                expires_at=now,
                created_at=now,
            )
            session.add(env)
            if pending != "new":
                await session.commit()
                if pending == "dirty":
                    env.name = "changed"
                else:
                    await session.delete(env)
            with pytest.raises(BookingError, match="pending mutations"):
                await _async_release_target(session, uuid4())
            # In particular the guard must not autoflush or discard caller state.
            assert env in getattr(session, pending)
            await session.rollback()
    finally:
        await engine.dispose()
