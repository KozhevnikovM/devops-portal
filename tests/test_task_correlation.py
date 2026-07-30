"""Regression tests for #371 F-3 — Celery task dispatch carries the request's correlation id.

Covers:
- `ReleaseBookingUseCase` threads an explicit `request_id` through to
  `TaskDispatcher.dispatch_teardown` (unit-level, same mocking style as
  `tests/test_force_release.py` / `tests/test_dispatcher_visibility.py`).
- A real HTTP request that dispatches a teardown (`DELETE /api/bookings/{id}`) carries the
  `X-Request-ID` — whether client-supplied or middleware-generated — through to the dispatcher
  call (mocking the dispatcher the same way `tests/test_static_vm_booking.py` does).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.presentation.middleware.correlation_id import REQUEST_ID_HEADER


def _user(role="user"):
    return User(id=uuid4(), username="someone", password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _ready_vm_booking(user_id):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=user_id, status=BookingStatus.READY, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


# ── Unit level: use case → dispatcher ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_release_booking_use_case_threads_request_id_to_dispatcher():
    user = _user()
    booking = _ready_vm_booking(str(user.id))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()

    await ReleaseBookingUseCase(repo, dispatcher).execute(
        AsyncMock(), booking.id, user, request_id="req-unit-test-1",
    )

    dispatcher.dispatch_teardown.assert_called_once_with(str(booking.id), request_id="req-unit-test-1")


@pytest.mark.asyncio
async def test_release_booking_use_case_defaults_request_id_to_none():
    """No request in flight (e.g. called from a non-HTTP context) — request_id stays None
    rather than silently requiring every caller to pass one."""
    user = _user()
    booking = _ready_vm_booking(str(user.id))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()

    await ReleaseBookingUseCase(repo, dispatcher).execute(AsyncMock(), booking.id, user)

    dispatcher.dispatch_teardown.assert_called_once_with(str(booking.id), request_id=None)


# ── HTTP level: route → get_request_id() → use case → TaskDispatcher ─────────

@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    fake_user = _user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


def test_release_dispatch_carries_client_supplied_request_id(user_client):
    client, user = user_client
    booking = _ready_vm_booking(str(user.id))

    from app.presentation.routes import api_bookings
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=booking)
    mock_repo.update_status = AsyncMock()
    mock_dispatcher = MagicMock()
    with patch.object(api_bookings._release_use_case, "_repo", mock_repo), \
         patch.object(api_bookings._release_use_case, "_dispatcher", mock_dispatcher):
        resp = client.delete(
            f"/api/bookings/{booking.id}", headers={REQUEST_ID_HEADER: "req-http-supplied"},
        )

    assert resp.status_code == 202
    assert resp.headers[REQUEST_ID_HEADER] == "req-http-supplied"
    mock_dispatcher.dispatch_teardown.assert_called_once_with(
        str(booking.id), request_id="req-http-supplied",
    )


def test_release_dispatch_carries_generated_request_id(user_client):
    """No client-supplied header — the middleware-generated id still reaches the dispatcher, and
    is the same one echoed back on the response."""
    client, user = user_client
    booking = _ready_vm_booking(str(user.id))

    from app.presentation.routes import api_bookings
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=booking)
    mock_repo.update_status = AsyncMock()
    mock_dispatcher = MagicMock()
    with patch.object(api_bookings._release_use_case, "_repo", mock_repo), \
         patch.object(api_bookings._release_use_case, "_dispatcher", mock_dispatcher):
        resp = client.delete(f"/api/bookings/{booking.id}")

    assert resp.status_code == 202
    echoed = resp.headers[REQUEST_ID_HEADER]
    assert echoed
    mock_dispatcher.dispatch_teardown.assert_called_once_with(str(booking.id), request_id=echoed)
