"""#434 — a booking that belongs to an environment can't be released on its own (Option B).

Enforced in ReleaseBookingUseCase, so the HTMX route, the versioned JSON API and the legacy JSON
API all return 409, and nothing is changed: no status write, no pool promotion, no teardown.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingPermissionError, EnvironmentChildReleaseError
from tests.conftest import make_fake_admin, make_fake_user

_CHILD_TYPES = [ResourceType.NAMESPACE, ResourceType.STATIC_VM, ResourceType.VM]


def _booking(status=BookingStatus.READY, resource_type=ResourceType.VM, environment_id=None,
             user_id="owner-1") -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=user_id, status=status, ttl_minutes=60,
        expires_at=now + timedelta(minutes=60), created_at=now,
        resource_type=resource_type, environment_id=environment_id,
    )


def _use_case(booking: Booking):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    repo.promote_next_queued = AsyncMock()
    dispatcher = MagicMock()
    return ReleaseBookingUseCase(repo, dispatcher), repo, dispatcher


def _owner(booking: Booking):
    user = make_fake_user()
    booking.user_id = str(user.id)
    return user


def _assert_untouched(repo, dispatcher):
    repo.update_status.assert_not_awaited()
    repo.promote_next_queued.assert_not_awaited()
    dispatcher.dispatch_teardown.assert_not_called()


# ── Use case ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", _CHILD_TYPES)
async def test_owner_cannot_release_ready_environment_child(resource_type):
    env_id = uuid4()
    booking = _booking(resource_type=resource_type, environment_id=env_id)
    uc, repo, dispatcher = _use_case(booking)
    with pytest.raises(EnvironmentChildReleaseError) as exc:
        await uc.execute(MagicMock(), booking.id, _owner(booking))
    assert str(exc.value) == f"Booking belongs to environment {env_id}; release the environment instead."
    _assert_untouched(repo, dispatcher)


@pytest.mark.asyncio
async def test_queued_environment_child_cannot_be_cancelled():
    booking = _booking(status=BookingStatus.QUEUED, resource_type=ResourceType.NAMESPACE,
                       environment_id=uuid4())
    uc, repo, dispatcher = _use_case(booking)
    with pytest.raises(EnvironmentChildReleaseError):
        await uc.execute(MagicMock(), booking.id, _owner(booking))
    _assert_untouched(repo, dispatcher)


@pytest.mark.asyncio
async def test_admin_cannot_force_delete_in_flight_environment_child():
    booking = _booking(status=BookingStatus.PROVISIONING, environment_id=uuid4())
    uc, repo, dispatcher = _use_case(booking)
    with pytest.raises(EnvironmentChildReleaseError):
        await uc.execute(MagicMock(), booking.id, make_fake_admin())
    _assert_untouched(repo, dispatcher)


@pytest.mark.asyncio
async def test_non_owner_still_gets_permission_error_not_environment_detail():
    booking = _booking(environment_id=uuid4(), user_id="someone-else")
    uc, repo, dispatcher = _use_case(booking)
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), booking.id, make_fake_user())
    _assert_untouched(repo, dispatcher)


@pytest.mark.asyncio
async def test_environment_release_force_path_still_releases_child():
    booking = _booking(resource_type=ResourceType.NAMESPACE, environment_id=uuid4())
    uc, repo, _ = _use_case(booking)
    await uc.execute(MagicMock(), booking.id, _owner(booking), force=True)
    repo.update_status.assert_awaited_once()
    assert repo.update_status.await_args.args[2] == BookingStatus.RELEASED
    repo.promote_next_queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_booking_release_unchanged():
    booking = _booking()
    uc, repo, dispatcher = _use_case(booking)
    await uc.execute(MagicMock(), booking.id, _owner(booking))
    assert repo.update_status.await_args.args[2] == BookingStatus.RELEASING
    dispatcher.dispatch_teardown.assert_called_once()


# ── Routes: HTMX, /api/v1 and legacy /api all map the rejection to 409 ─────────────────────────
@pytest.fixture
def owner_client():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    user = make_fake_user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    yield TestClient(app), user
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/bookings/{id}", "/api/v1/bookings/{id}", "/api/bookings/{id}"])
@pytest.mark.parametrize("resource_type", _CHILD_TYPES)
def test_release_route_returns_409_for_environment_child(owner_client, path, resource_type):
    from app.presentation.routes import api_bookings
    client, user = owner_client
    env_id = uuid4()
    booking = _booking(resource_type=resource_type, environment_id=env_id, user_id=str(user.id))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    repo.promote_next_queued = AsyncMock()
    dispatcher = MagicMock()
    # The HTMX and JSON routers share the one ReleaseBookingUseCase instance (deps.release_booking_uc).
    with patch.object(api_bookings._release_use_case, "_repo", repo), \
         patch.object(api_bookings._release_use_case, "_dispatcher", dispatcher):
        resp = client.delete(path.format(id=booking.id))
    assert resp.status_code == 409
    assert str(env_id) in resp.json()["detail"]
    assert "release the environment instead" in resp.json()["detail"]
    assert booking.status == BookingStatus.READY
    _assert_untouched(repo, dispatcher)
