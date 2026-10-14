"""#532 — an environment child can't be extended on its own through the booking extend operation:
the use case, the authoritative locked recheck in BookingRepository.extend, every route (HTMX,
/api/v1, legacy /api) and the booking row. Standalone bookings — "Forever" included — unchanged."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError,
    BookingPermissionError,
    EnvironmentChildExtendError,
)
from app.infrastructure.repositories.booking_repo import BookingRepository

_NOW = datetime.now(UTC)
_ENV_ID = uuid4()


def _user(role="user") -> User:
    return User(
        id=uuid4(),
        username=role,
        password_hash="",
        role=role,
        is_active=True,
        created_at=_NOW,
    )


def _booking(
    owner: User, status=BookingStatus.READY, environment_id=_ENV_ID, ttl_minutes=240
):
    return Booking(
        id=uuid4(),
        user_id=str(owner.id),
        status=status,
        ttl_minutes=ttl_minutes,
        expires_at=_NOW + timedelta(minutes=240),
        created_at=_NOW,
        resource_type=ResourceType.NAMESPACE,
        namespace_name="ns1",
        environment_id=environment_id,
        owner_username=owner.username,
    )


def _uc(booking):
    repo = MagicMock(spec=BookingRepository)
    repo.get = AsyncMock(return_value=booking)
    repo.extend = AsyncMock()
    return ExtendBookingUseCase(repo), repo


# ── Use case ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "dispatcher", "admin"])
@pytest.mark.parametrize("minutes", [720, 0], ids=["timed", "forever"])
async def test_child_extension_is_rejected(role, minutes):
    actor = _user(role)
    child = _booking(actor)
    uc, repo = _uc(child)
    with pytest.raises(
        EnvironmentChildExtendError,
        match=f"environment {_ENV_ID}; extend the environment",
    ):
        await uc.execute(MagicMock(), child.id, minutes, actor)
    repo.extend.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [BookingStatus.PROVISIONING, BookingStatus.FAILED, BookingStatus.QUEUED]
)
async def test_child_is_rejected_in_every_status(status):
    owner = _user()
    uc, _ = _uc(_booking(owner, status=status))
    with pytest.raises(EnvironmentChildExtendError):
        await uc.execute(MagicMock(), uuid4(), 60, owner)


@pytest.mark.asyncio
async def test_unauthorized_caller_still_gets_403_first():
    uc, _ = _uc(_booking(_user()))
    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), uuid4(), 60, _user())


def test_child_error_is_a_booking_error():
    # So every existing booking-extend route maps it to 409 unchanged.
    assert issubclass(EnvironmentChildExtendError, BookingError)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [720, 0], ids=["timed", "forever"])
async def test_standalone_booking_still_extends(minutes):
    owner = _user()
    booking = _booking(owner, environment_id=None)
    uc, repo = _uc(booking)
    await uc.execute(MagicMock(), booking.id, minutes, owner)
    repo.extend.assert_awaited_once()


# ── Repository: authoritative recheck under the row lock ────────────────────────
def _session_returning(model):
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _model(**overrides):
    fields = {
        "id": uuid4(),
        "environment_id": None,
        "status": BookingStatus.READY.value,
        "ttl_minutes": 60,
        "expires_at": _NOW,
        "user_id": "u",
        "created_by": None,
    }
    fields.update(overrides)
    return MagicMock(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"environment_id": _ENV_ID}, EnvironmentChildExtendError),
        ({"status": BookingStatus.RELEASING.value}, BookingError),
        ({"ttl_minutes": 0}, BookingError),
    ],
    ids=["adopted-since", "released-since", "permanent"],
)
async def test_repo_extend_rechecks_under_lock(overrides, error):
    model = _model(**overrides)
    session = _session_returning(model)

    with pytest.raises(error):
        await BookingRepository().extend(session, model.id, 60, actor_id="a")

    stmt = session.execute.await_args.args[0]
    assert stmt._for_update_arg is not None  # SELECT … FOR UPDATE
    session.add.assert_not_called()  # no audit entry
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_repo_extend_standalone_writes_and_audits():
    model = _model()
    session = _session_returning(model)
    await BookingRepository().extend(session, model.id, 60, actor_id="a")
    assert model.ttl_minutes == 120
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_environment_locks_the_row():
    model = _model()
    session = _session_returning(model)
    await BookingRepository().set_environment(
        session, model.id, _ENV_ID, "ns", 60, _NOW
    )
    assert session.execute.await_args.args[0]._for_update_arg is not None


# ── Routes ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def client_for():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()

    def make(user):
        app.dependency_overrides[require_user] = lambda: user
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path, module",
    [
        ("/api/v1/bookings/{id}/extend", "api_bookings"),
        ("/api/bookings/{id}/extend", "api_bookings"),
    ],
    ids=["v1", "legacy"],
)
def test_api_child_extension_409(client_for, path, module):
    owner = _user()
    child = _booking(owner)
    uc, repo = _uc(child)
    with patch(f"app.presentation.routes.{module}._extend_use_case", uc):
        resp = client_for(owner).put(
            path.format(id=child.id), json={"extend_minutes": 720}
        )
    assert resp.status_code == 409
    assert str(_ENV_ID) in resp.json()["detail"]
    assert "extend the environment instead" in resp.json()["detail"]
    repo.extend.assert_not_called()


def test_api_standalone_extension_unchanged(client_for):
    owner = _user()
    booking = _booking(owner, environment_id=None)
    uc, repo = _uc(booking)
    with patch("app.presentation.routes.api_bookings._extend_use_case", uc):
        resp = client_for(owner).put(
            f"/api/v1/bookings/{booking.id}/extend", json={"extend_minutes": 0}
        )
    assert resp.status_code == 200
    assert set(resp.json()) == {"id", "status", "ttl_minutes", "expires_at"}
    repo.extend.assert_awaited_once()


def test_htmx_child_extension_409(client_for):
    owner = _user()
    child = _booking(owner)
    uc, repo = _uc(child)
    with patch("app.presentation.routes.bookings._extend_use_case", uc):
        resp = client_for(owner).put(
            f"/bookings/{child.id}/extend", data={"extend_minutes": "720"}
        )
    assert resp.status_code == 409
    repo.extend.assert_not_called()


# ── Booking row ─────────────────────────────────────────────────────────────────
def _row(client, booking) -> str:
    with patch("app.presentation.routes.bookings._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        repo.queue_positions = AsyncMock(return_value={})
        resp = client.get(f"/bookings/{booking.id}/row")
    assert resp.status_code == 200
    return resp.text


def test_child_row_has_no_extend_and_points_to_environment(client_for):
    owner = _user()
    child = _booking(owner)
    html = _row(client_for(owner), child)
    assert f'hx-put="/bookings/{child.id}/extend"' not in html
    assert "data-environment-managed" in html
    assert "extend or release it there" in html


def test_standalone_row_keeps_extend_with_forever(client_for):
    owner = _user()
    booking = _booking(owner, environment_id=None)
    html = _row(client_for(owner), booking)
    assert f'hx-put="/bookings/{booking.id}/extend"' in html
    assert ">Forever<" in html
