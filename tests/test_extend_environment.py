"""ExtendEnvironmentUseCase — authorization, eligibility and increment rules (#532)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.extend_environment import ExtendEnvironmentUseCase
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingPermissionError,
    EnvironmentError,
    EnvironmentNotFoundError,
    InvalidExtensionError,
)

_OWNER = UUID(int=1)
_DISPATCHER = UUID(int=2)
_NOW = datetime.now(UTC)


def _user(uid=None, role="user") -> User:
    return User(
        id=uid or uuid4(),
        username="u",
        password_hash="",
        role=role,
        is_active=True,
        created_at=_NOW,
    )


def _child(status=BookingStatus.READY, rt=ResourceType.NAMESPACE) -> Booking:
    return Booking(
        id=uuid4(),
        user_id=str(_OWNER),
        status=status,
        resource_type=rt,
        ttl_minutes=60,
        expires_at=_NOW + timedelta(minutes=60),
        created_at=_NOW,
    )


def _env(*children, ttl_minutes=60, expires_at=None) -> Environment:
    return Environment(
        id=uuid4(),
        name="stack",
        blueprint_name="bp",
        user_id=str(_OWNER),
        ttl_minutes=ttl_minutes,
        expires_at=expires_at or _NOW + timedelta(minutes=60),
        created_at=_NOW,
        bookings=list(children or [_child()]),
        created_by=str(_DISPATCHER),
    )


def _uc(env):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=env)
    repo.extend_lease = AsyncMock()
    return ExtendEnvironmentUseCase(repo), repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        _user(_OWNER),
        _user(_DISPATCHER, role="dispatcher"),
        _user(role="admin"),
    ],
    ids=["owner", "creating-dispatcher", "admin"],
)
async def test_managers_extend_the_stack(user):
    env = _env(_child(), _child(rt=ResourceType.VM))
    uc, repo = _uc(env)
    session = MagicMock()

    result = await uc.execute(session, env.id, 720, user)

    repo.extend_lease.assert_awaited_once_with(
        session, env.id, 720, actor_id=str(user.id)
    )
    assert result is env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [BookingStatus.READY, BookingStatus.PROVISIONING, BookingStatus.RELEASED]
)
async def test_unrelated_user_is_forbidden_in_any_status(status):
    uc, repo = _uc(_env(_child(status)))
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), uuid4(), 60, _user(role="dispatcher"))
    repo.extend_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_forbidden_even_for_invalid_increment():
    uc, _ = _uc(_env())
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), uuid4(), 0, _user())


@pytest.mark.asyncio
async def test_missing_environment():
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
    with pytest.raises(EnvironmentNotFoundError):
        await ExtendEnvironmentUseCase(repo).execute(
            MagicMock(), uuid4(), 60, _user(_OWNER)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, -30])
async def test_nonpositive_increment(minutes):
    uc, repo = _uc(_env())
    with pytest.raises(InvalidExtensionError):
        await uc.execute(MagicMock(), uuid4(), minutes, _user(_OWNER))
    repo.extend_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_environment():
    uc, repo = _uc(_env(ttl_minutes=0, expires_at=PERMANENT_EXPIRES_AT))
    with pytest.raises(EnvironmentError, match="permanent"):
        await uc.execute(MagicMock(), uuid4(), 60, _user(_OWNER))
    repo.extend_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_not_started():
    uc, repo = _uc(_env(expires_at=PERMANENT_EXPIRES_AT))
    with pytest.raises(EnvironmentError, match="not started"):
        await uc.execute(MagicMock(), uuid4(), 60, _user(_OWNER))
    repo.extend_lease.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "children",
    [
        (BookingStatus.READY, BookingStatus.PROVISIONING),
        (BookingStatus.READY, BookingStatus.FAILED),
        (BookingStatus.READY, BookingStatus.RELEASING),
        (BookingStatus.RELEASED, BookingStatus.RELEASED),
    ],
    ids=["provisioning", "failed", "partly-released", "released"],
)
async def test_ineligible_stack(children):
    uc, repo = _uc(_env(*(_child(s) for s in children)))
    with pytest.raises(EnvironmentError, match="READY"):
        await uc.execute(MagicMock(), uuid4(), 60, _user(_OWNER))
    repo.extend_lease.assert_not_awaited()
