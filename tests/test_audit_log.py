import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.database.models import BookingAuditModel
from app.infrastructure.repositories.booking_repo import BookingRepository


def _make_booking_model(status: str = "PENDING"):
    import uuid
    from app.infrastructure.database.models import BookingModel
    now = datetime.now(timezone.utc)
    return BookingModel(
        id=uuid.uuid4(),
        user_id="dev-user",
        status=status,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        image_id=uuid.uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid.uuid4(),
        hw_config_name="medium",
        vm_ip=None,
        created_at=now,
    )


def _make_booking_entity(status: BookingStatus = BookingStatus.PENDING) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id="dev-user",
        status=status,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


def _make_audit_entry(action: str = "STATUS_CHANGED", **kwargs) -> BookingAuditEntry:
    return BookingAuditEntry(
        id=uuid4(),
        booking_id=kwargs.get("booking_id", uuid4()),
        actor_id=kwargs.get("actor_id", "system"),
        action=action,
        old_status=kwargs.get("old_status", "PENDING"),
        new_status=kwargs.get("new_status", "PROVISIONING"),
        metadata=kwargs.get("metadata", None),
        created_at=kwargs.get("created_at", datetime.now(timezone.utc)),
    )


# ---------------------------------------------------------------------------
# Repository unit tests — mock the session, inspect session.add() calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_booking_writes_created_audit():
    booking = _make_booking_entity()
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    repo = BookingRepository()
    await repo.create(session, booking)

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "CREATED"
    assert audit_rows[0].booking_id == booking.id
    assert audit_rows[0].actor_id == booking.user_id
    assert audit_rows[0].old_status is None
    assert audit_rows[0].new_status is None


@pytest.mark.asyncio
async def test_update_status_writes_status_changed_audit():
    model = _make_booking_model("PENDING")

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = model

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    repo = BookingRepository()
    await repo.update_status(session, model.id, BookingStatus.PROVISIONING)

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "STATUS_CHANGED"
    assert audit_rows[0].old_status == "PENDING"
    assert audit_rows[0].new_status == "PROVISIONING"


@pytest.mark.asyncio
async def test_update_status_writes_vm_ip_to_metadata():
    model = _make_booking_model("PROVISIONING")

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = model

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    repo = BookingRepository()
    await repo.update_status(session, model.id, BookingStatus.READY, vm_ip="10.0.0.1")

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert audit_rows[0].extra == {"vm_ip": "10.0.0.1"}


@pytest.mark.asyncio
async def test_audit_actor_id_defaults_to_system():
    model = _make_booking_model("READY")

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = model

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    repo = BookingRepository()
    await repo.update_status(session, model.id, BookingStatus.RELEASING)

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert audit_rows[0].actor_id == "system"


@pytest.mark.asyncio
async def test_update_status_actor_id_passed_through():
    model = _make_booking_model("READY")

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = model

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    repo = BookingRepository()
    await repo.update_status(session, model.id, BookingStatus.RELEASING, actor_id="dev-user")

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert audit_rows[0].actor_id == "dev-user"


def test_sync_update_status_writes_audit():
    from sqlalchemy.orm import Session

    model = _make_booking_model("READY")
    session = MagicMock(spec=Session)
    session.get.return_value = model

    repo = BookingRepository()
    repo.sync_update_status(session, model.id, BookingStatus.RELEASING)

    added = [c.args[0] for c in session.add.call_args_list]
    audit_rows = [o for o in added if isinstance(o, BookingAuditModel)]
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "STATUS_CHANGED"
    assert audit_rows[0].old_status == "READY"
    assert audit_rows[0].new_status == "RELEASING"


# ---------------------------------------------------------------------------
# API endpoint tests — FastAPI TestClient with mocked repo
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.database.session import get_async_session
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    yield TestClient(app)
    app.dependency_overrides.clear()


from fastapi.testclient import TestClient


@pytest.fixture
def api_client():
    from app.main import app
    from app.infrastructure.database.session import get_async_session
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_get_audit_returns_200_with_entries(api_client):
    booking_id = uuid4()
    now = datetime.now(timezone.utc)
    entries = [
        _make_audit_entry("CREATED", booking_id=booking_id, actor_id="dev-user",
                          old_status=None, new_status=None, created_at=now),
        _make_audit_entry("STATUS_CHANGED", booking_id=booking_id, actor_id="system",
                          old_status="PENDING", new_status="PROVISIONING",
                          created_at=now + timedelta(seconds=5)),
    ]

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock()
        mock_repo.list_audit = AsyncMock(return_value=entries)

        resp = api_client.get(f"/bookings/{booking_id}/audit")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["action"] == "CREATED"
    assert data[1]["action"] == "STATUS_CHANGED"
    assert data[1]["old_status"] == "PENDING"
    assert data[1]["new_status"] == "PROVISIONING"


def test_get_audit_returns_404_for_missing_booking(api_client):
    booking_id = uuid4()

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(side_effect=BookingNotFoundError(booking_id))

        resp = api_client.get(f"/bookings/{booking_id}/audit")

    assert resp.status_code == 404


def test_get_audit_entries_have_expected_fields(api_client):
    booking_id = uuid4()
    entry = _make_audit_entry(
        "STATUS_CHANGED",
        booking_id=booking_id,
        actor_id="system",
        old_status="PROVISIONING",
        new_status="READY",
        metadata={"vm_ip": "10.0.0.5"},
    )

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock()
        mock_repo.list_audit = AsyncMock(return_value=[entry])

        resp = api_client.get(f"/bookings/{booking_id}/audit")

    data = resp.json()
    assert len(data) == 1
    row = data[0]
    assert set(row.keys()) == {"id", "booking_id", "action", "old_status", "new_status", "actor_id", "metadata", "created_at"}
    assert row["metadata"] == {"vm_ip": "10.0.0.5"}
    assert row["actor_id"] == "system"
