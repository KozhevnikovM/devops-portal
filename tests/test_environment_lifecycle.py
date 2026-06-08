"""Tests for environment lifecycle — grouped release & TTL (v0.8.0 P3.3, #210)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from uuid import UUID

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingError, BookingPermissionError

_OWNER = UUID(int=0)
_OWNER_ID = str(_OWNER)


def _booking(rt=ResourceType.VM, status=BookingStatus.READY, user_id=_OWNER_ID):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=user_id, status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
    )


def _env(children, user_id=_OWNER_ID):
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=user_id,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now, bookings=children,
    )


def _owner(role="user"):
    return User(id=_OWNER, username="u", password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _other_user():
    return User(id=uuid4(), username="other", password_hash="", role="user",
                is_active=True, created_at=datetime.now(timezone.utc))


# ── ReleaseBookingUseCase force flag ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_force_releases_in_flight_booking():
    from app.application.use_cases.release_booking import ReleaseBookingUseCase
    b = _booking(ResourceType.VM, BookingStatus.PROVISIONING)
    releasing = _booking(ResourceType.VM, BookingStatus.RELEASING)
    releasing.id = b.id
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[b, releasing])
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()
    uc = ReleaseBookingUseCase(repo, dispatcher)
    # A normal owner release would 409 on PROVISIONING; force releases it.
    await uc.execute(MagicMock(), b.id, _owner(), force=True)
    dispatcher.dispatch_teardown.assert_called_once()


# ── ReleaseEnvironmentUseCase ──────────────────────────────────────────────────
def _release_uc(env, get_after=None):
    env_repo = MagicMock()
    env_repo.get = AsyncMock(side_effect=[env, get_after or env])
    release_booking = MagicMock()
    release_booking.execute = AsyncMock()
    from app.application.use_cases.release_environment import ReleaseEnvironmentUseCase
    return ReleaseEnvironmentUseCase(env_repo, release_booking), env_repo, release_booking


@pytest.mark.asyncio
async def test_release_environment_releases_all_live_children():
    children = [
        _booking(ResourceType.NAMESPACE, BookingStatus.READY),
        _booking(ResourceType.VM, BookingStatus.PROVISIONING),
        _booking(ResourceType.VM, BookingStatus.RELEASED),   # terminal — skipped
    ]
    env = _env(children)
    uc, env_repo, release_booking = _release_uc(env)
    await uc.execute(MagicMock(), env.id, _owner())
    # Two live children released (force); the terminal one skipped.
    assert release_booking.execute.await_count == 2
    assert all(c.kwargs.get("force") for c in release_booking.execute.call_args_list)


@pytest.mark.asyncio
async def test_release_environment_non_owner_403():
    env = _env([_booking(user_id='someone-else')], user_id='someone-else')
    uc, env_repo, release_booking = _release_uc(env)
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), env.id, _other_user())
    release_booking.execute.assert_not_called()


@pytest.mark.asyncio
async def test_release_environment_missing_404():
    from app.application.use_cases.release_environment import EnvironmentNotFoundError, ReleaseEnvironmentUseCase
    env_repo = MagicMock()
    env_repo.get = AsyncMock(side_effect=ValueError("nope"))
    uc = ReleaseEnvironmentUseCase(env_repo, MagicMock())
    with pytest.raises(EnvironmentNotFoundError):
        await uc.execute(MagicMock(), uuid4(), _owner(role='admin'))


# ── TTL enforcement: env-aware ──────────────────────────────────────────────────
def test_enforce_ttl_skips_env_children():
    """The per-booking enforce_ttl query excludes environment children."""
    import inspect
    from app.infrastructure.repositories import booking_repo as br
    src = inspect.getsource(br.BookingRepository.sync_list_expired)
    assert "environment_id.is_(None)" in src


def test_enforce_environment_ttl_releases_children():
    from app.tasks import beat_tasks
    env = _env([
        _booking(ResourceType.NAMESPACE, BookingStatus.READY),
        _booking(ResourceType.VM, BookingStatus.READY),
    ])
    children = env.bookings
    mock_repo = MagicMock()
    mock_env_repo = MagicMock()
    mock_env_repo.sync_list_expired = MagicMock(return_value=[env])
    mock_env_repo.sync_live_children = MagicMock(return_value=children)
    teardown = MagicMock()
    with (
        patch("app.tasks.beat_tasks.SyncSessionLocal") as sf,
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.env_repo", mock_env_repo),
        patch("app.tasks.teardown.teardown_vm_task", teardown),
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        beat_tasks.enforce_environment_ttl()

    # Namespace → RELEASED + promote; VM → RELEASING + teardown dispatched.
    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASED in statuses   # namespace
    assert BookingStatus.RELEASING in statuses  # VM
    mock_repo.sync_promote_next_queued.assert_called()  # pooled promote
    teardown.delay.assert_called_once()         # VM teardown


# ── API: DELETE /api/environments/{id} ──────────────────────────────────────────
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_release_environment_202(client):
    env = _env([_booking(ResourceType.VM, BookingStatus.RELEASING)])
    with patch("app.presentation.routes.api_environments._release_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.delete(f"/api/environments/{env.id}")
    assert resp.status_code == 202


def test_api_release_environment_404(client):
    from app.application.use_cases.release_environment import EnvironmentNotFoundError
    with patch("app.presentation.routes.api_environments._release_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
        resp = client.delete(f"/api/environments/{uuid4()}")
    assert resp.status_code == 404
