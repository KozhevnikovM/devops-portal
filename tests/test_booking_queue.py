from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType


def _booking(status, resource_type=ResourceType.STATIC_VM, **kw) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=kw.get("id", uuid4()),
        user_id=kw.get("user_id", "dev-user"),
        status=status,
        resource_type=resource_type,
        ttl_minutes=kw.get("ttl_minutes", 240),
        expires_at=now,
        created_at=now,
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    session_mock = AsyncMock()
    fake_user = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


# ── enum ──────────────────────────────────────────────────────────────────────

def test_queued_status_exists():
    assert BookingStatus.QUEUED.value == "QUEUED"


# ── Use cases enqueue when the pool is empty ────────────────────────────────────

@pytest.mark.asyncio
async def test_namespace_use_case_queues_when_empty():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.lock_next_available = AsyncMock(return_value=None)

    booking = await BookNamespaceUseCase(repo, ns_repo).execute(AsyncMock(), 240, user_id="u1")

    assert booking.status == BookingStatus.QUEUED
    assert booking.resource_type == ResourceType.NAMESPACE
    assert booking.namespace_id is None
    repo.create.assert_awaited_once()


# ── promote_next_queued (repo) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_assigns_resource_and_sets_ready():
    from app.infrastructure.repositories import booking_repo as mod

    queued = SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240,
        static_vm_id=None, namespace_id=None, expires_at=None,
    )
    free_vm = SimpleNamespace(id=uuid4(), name="vm-1")

    session = AsyncMock()
    # first execute() → oldest queued booking; second → free resource
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: free_vm
    session.execute = AsyncMock(side_effect=[r1, r2])
    session.add = MagicMock()
    session.refresh = AsyncMock()

    with patch.object(mod, "_to_entity", lambda m: m):
        await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)

    assert queued.status == BookingStatus.READY.value
    assert queued.static_vm_id == free_vm.id
    assert queued.expires_at is not None        # TTL started on promotion
    session.add.assert_called_once()            # STATUS_CHANGED audit row
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_noop_when_no_queued():
    from app.infrastructure.repositories import booking_repo as mod

    session = AsyncMock()
    r = MagicMock(); r.scalar_one_or_none = lambda: None
    session.execute = AsyncMock(return_value=r)

    out = await mod.BookingRepository().promote_next_queued(session, ResourceType.NAMESPACE.value)
    assert out is None
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_promote_noop_when_no_free_resource():
    from app.infrastructure.repositories import booking_repo as mod

    queued = SimpleNamespace(id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=10,
                             static_vm_id=None, namespace_id=None, expires_at=None)
    session = AsyncMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: None  # nothing free
    session.execute = AsyncMock(side_effect=[r1, r2])

    out = await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)
    assert out is None
    assert queued.status == BookingStatus.QUEUED.value  # untouched
    session.commit.assert_not_called()


# ── Cancel a queued booking ─────────────────────────────────────────────────────

def test_cancel_queued_booking_releases_without_promote(client):
    cl, fake_user = client
    queued = _booking(BookingStatus.QUEUED, user_id=str(fake_user.id))
    released = _booking(BookingStatus.RELEASED, id=queued.id, user_id=str(fake_user.id))

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.tasks.teardown.teardown_vm_task") as mock_task:
        mock_repo.get = AsyncMock(side_effect=[queued, released])
        mock_repo.update_status = AsyncMock()
        mock_repo.promote_next_queued = AsyncMock()
        resp = cl.delete(f"/bookings/{queued.id}", headers={"Accept": "application/json"})

    assert resp.status_code == 202
    assert resp.json()["status"] == "RELEASED"
    assert mock_repo.update_status.call_args.args[2] == BookingStatus.RELEASED
    mock_task.delay.assert_not_called()              # nothing to tear down
    mock_repo.promote_next_queued.assert_not_called()  # cancelling frees no resource


# ── Teardown/TTL path promotes the next queued booking ──────────────────────────

def test_teardown_pooled_release_promotes_next():
    from app.tasks import teardown as teardown_mod

    booking = _booking(BookingStatus.READY, resource_type=ResourceType.STATIC_VM)
    with patch.object(teardown_mod, "repo") as mock_repo, \
         patch.object(teardown_mod, "terraform") as mock_tf, \
         patch.object(teardown_mod, "SyncSessionLocal") as mock_sl:
        mock_sl.return_value.__enter__.return_value = MagicMock()
        mock_repo.sync_get = MagicMock(return_value=booking)
        mock_repo.sync_update_status = MagicMock()
        mock_repo.sync_promote_next_queued = MagicMock()

        teardown_mod.teardown_vm_task.run(str(booking.id))

    assert mock_repo.sync_update_status.call_args.args[2] == BookingStatus.RELEASED
    mock_repo.sync_promote_next_queued.assert_called_once_with(
        mock_sl.return_value.__enter__.return_value, ResourceType.STATIC_VM.value
    )
    mock_tf.destroy.assert_not_called()


# ── QUEUED row rendering ────────────────────────────────────────────────────────

def test_queued_row_shows_position_and_cancel(client):
    cl, fake_user = client
    queued = _booking(BookingStatus.QUEUED, user_id=str(fake_user.id))
    queued.owner_username = fake_user.username
    queued.queue_position = 2

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=queued)
        mock_repo.queue_position = AsyncMock(return_value=2)
        resp = cl.get(f"/bookings/{queued.id}/row")

    assert resp.status_code == 200
    assert "Queued — position 2" in resp.text
    assert "Cancel" in resp.text
    # QUEUED is non-terminal → keeps polling for promotion
    assert 'hx-get="/bookings/' in resp.text
