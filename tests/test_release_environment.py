"""Unit tests for ReleaseEnvironmentUseCase (#452)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.application.use_cases.release_environment import ReleaseEnvironmentUseCase
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingPermissionError


def _make_user(role="user", user_id=None):
    return User(
        id=user_id or uuid4(),
        username="test-user",
        password_hash="",
        role=role,
        is_active=True,
        created_at=datetime.now(UTC),
    )


def _make_booking(
    status=BookingStatus.READY, resource_type=ResourceType.VM, user_id=None
):
    now = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        user_id=str(user_id or uuid4()),
        status=status,
        resource_type=resource_type,
        ttl_minutes=120,
        expires_at=now + timedelta(minutes=120),
        created_at=now,
    )


def _make_env(children, user_id=None):
    now = datetime.now(UTC)
    uid = str(user_id or uuid4())
    return Environment(
        id=uuid4(),
        name="test-env",
        blueprint_name="test-bp",
        user_id=uid,
        ttl_minutes=120,
        expires_at=now + timedelta(minutes=120),
        created_at=now,
        bookings=children,
    )


@pytest.mark.asyncio
async def test_release_environment_releases_failed_child_booking():
    owner = _make_user()
    child_failed = _make_booking(
        status=BookingStatus.FAILED, resource_type=ResourceType.VM, user_id=owner.id
    )
    child_ready = _make_booking(
        status=BookingStatus.READY,
        resource_type=ResourceType.NAMESPACE,
        user_id=owner.id,
    )
    env = _make_env([child_failed, child_ready], user_id=owner.id)

    env_repo = MagicMock()
    env_repo.get = AsyncMock(return_value=env)
    release_booking = MagicMock()
    release_booking.execute = AsyncMock()

    uc = ReleaseEnvironmentUseCase(env_repo, release_booking)
    result = await uc.execute(MagicMock(), env.id, owner)

    assert result.id == env.id
    # Both FAILED and READY children must be released with force=True
    assert release_booking.execute.await_count == 2
    called_ids = {call.args[1] for call in release_booking.execute.call_args_list}
    assert called_ids == {child_failed.id, child_ready.id}
    assert all(
        call.kwargs.get("force") is True
        for call in release_booking.execute.call_args_list
    )


@pytest.mark.asyncio
async def test_release_environment_skips_released_child():
    owner = _make_user()
    child_released = _make_booking(
        status=BookingStatus.RELEASED, resource_type=ResourceType.VM, user_id=owner.id
    )
    child_ready = _make_booking(
        status=BookingStatus.READY,
        resource_type=ResourceType.STATIC_VM,
        user_id=owner.id,
    )
    env = _make_env([child_released, child_ready], user_id=owner.id)

    env_repo = MagicMock()
    env_repo.get = AsyncMock(return_value=env)
    release_booking = MagicMock()
    release_booking.execute = AsyncMock()

    uc = ReleaseEnvironmentUseCase(env_repo, release_booking)
    result = await uc.execute(MagicMock(), env.id, owner)

    assert result.id == env.id
    # Only the non-RELEASED child is released
    assert release_booking.execute.await_count == 1
    assert release_booking.execute.call_args.args[1] == child_ready.id
    assert release_booking.execute.call_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_release_environment_releases_mixed_non_terminal_children():
    owner = _make_user()
    children = [
        _make_booking(
            status=BookingStatus.PROVISIONING,
            resource_type=ResourceType.VM,
            user_id=owner.id,
        ),
        _make_booking(
            status=BookingStatus.CONFIGURING,
            resource_type=ResourceType.VM,
            user_id=owner.id,
        ),
        _make_booking(
            status=BookingStatus.FAILED, resource_type=ResourceType.VM, user_id=owner.id
        ),
        _make_booking(
            status=BookingStatus.READY,
            resource_type=ResourceType.NAMESPACE,
            user_id=owner.id,
        ),
        _make_booking(
            status=BookingStatus.RELEASED,
            resource_type=ResourceType.STATIC_VM,
            user_id=owner.id,
        ),
    ]
    env = _make_env(children, user_id=owner.id)

    env_repo = MagicMock()
    env_repo.get = AsyncMock(return_value=env)
    release_booking = MagicMock()
    release_booking.execute = AsyncMock()

    uc = ReleaseEnvironmentUseCase(env_repo, release_booking)
    await uc.execute(MagicMock(), env.id, owner)

    # 4 non-RELEASED children released; RELEASED child skipped
    assert release_booking.execute.await_count == 4
    released_ids = {call.args[1] for call in release_booking.execute.call_args_list}
    assert children[4].id not in released_ids


@pytest.mark.asyncio
async def test_release_environment_permission_check():
    owner = _make_user()
    stranger = _make_user()
    env = _make_env([_make_booking(user_id=owner.id)], user_id=owner.id)

    env_repo = MagicMock()
    env_repo.get = AsyncMock(return_value=env)
    release_booking = MagicMock()
    release_booking.execute = AsyncMock()

    uc = ReleaseEnvironmentUseCase(env_repo, release_booking)
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), env.id, stranger)

    release_booking.execute.assert_not_called()
