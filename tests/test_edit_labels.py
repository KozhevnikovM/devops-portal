"""Tests for #342: editing a VM booking's label and an Environment's name after creation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError, BookingNotFoundError, BookingPermissionError,
    EnvironmentError, EnvironmentNotFoundError,
)
from app.application.use_cases.update_booking_label import UpdateBookingLabelUseCase
from app.application.use_cases.update_environment_name import UpdateEnvironmentNameUseCase

_OWNER = UUID(int=0)
_OWNER_ID = str(_OWNER)


def _owner(role="user"):
    return User(id=_OWNER, username="alice", password_hash="x", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _other_user(role="user"):
    return User(id=uuid4(), username="bob", password_hash="x", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def _booking(status=BookingStatus.READY, user_id=_OWNER_ID, created_by=None, label=None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=user_id, status=status, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        created_by=created_by, label=label,
    )


def _env(user_id=_OWNER_ID, created_by=None, name="dev-stack"):
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name=name, blueprint_name="dev-stack", user_id=user_id,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        created_by=created_by,
    )


# ── UpdateBookingLabelUseCase ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_can_relabel_booking():
    booking = _booking(label="old label")
    relabeled = _booking(label="new label")
    relabeled.id = booking.id
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, relabeled])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)
    session = MagicMock()

    result = await uc.execute(session, booking.id, "new label", _owner())

    repo.update_label.assert_called_once_with(session, booking.id, "new label", actor_id=_OWNER_ID)
    assert result.label == "new label"


@pytest.mark.asyncio
async def test_relabel_strips_whitespace():
    booking = _booking()
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, booking])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    await uc.execute(MagicMock(), booking.id, "  padded  ", _owner())

    assert repo.update_label.call_args.args[2] == "padded"


@pytest.mark.asyncio
async def test_relabel_blank_clears_label():
    booking = _booking(label="something")
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, booking])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    await uc.execute(MagicMock(), booking.id, "   ", _owner())

    assert repo.update_label.call_args.args[2] is None


@pytest.mark.asyncio
async def test_relabel_over_128_chars_raises_400():
    booking = _booking()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    with pytest.raises(BookingError, match="128"):
        await uc.execute(MagicMock(), booking.id, "x" * 129, _owner())

    repo.update_label.assert_not_called()


@pytest.mark.asyncio
async def test_relabel_wrong_owner_raises_403():
    booking = _booking(user_id=str(uuid4()))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), booking.id, "new label", _other_user())

    repo.update_label.assert_not_called()


@pytest.mark.asyncio
async def test_admin_can_relabel_any_booking():
    booking = _booking(user_id=str(uuid4()))
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, booking])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    await uc.execute(MagicMock(), booking.id, "admin label", _other_user(role="admin"))

    repo.update_label.assert_called_once()


@pytest.mark.asyncio
async def test_creating_dispatcher_can_relabel():
    dispatcher = _other_user()
    booking = _booking(user_id=str(uuid4()), created_by=str(dispatcher.id))
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, booking])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    await uc.execute(MagicMock(), booking.id, "dispatched label", dispatcher)

    repo.update_label.assert_called_once()


@pytest.mark.asyncio
async def test_relabel_allowed_regardless_of_status():
    """A label is a display annotation, not operational state — no status guard."""
    booking = _booking(status=BookingStatus.RELEASED)
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, booking])
    repo.update_label = AsyncMock()
    uc = UpdateBookingLabelUseCase(repo)

    await uc.execute(MagicMock(), booking.id, "post-release note", _owner())

    repo.update_label.assert_called_once()


# ── UpdateEnvironmentNameUseCase ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_can_rename_environment():
    env = _env(name="old name")
    renamed = _env(name="new name")
    renamed.id = env.id
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[env, renamed])
    repo.update_name = AsyncMock()
    uc = UpdateEnvironmentNameUseCase(repo)

    result = await uc.execute(MagicMock(), env.id, "new name", _owner())

    repo.update_name.assert_called_once_with(repo.update_name.call_args[0][0], env.id, "new name")
    assert result.name == "new name"


@pytest.mark.asyncio
async def test_rename_blank_raises_400():
    env = _env()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=env)
    repo.update_name = AsyncMock()
    uc = UpdateEnvironmentNameUseCase(repo)

    with pytest.raises(EnvironmentError, match="blank"):
        await uc.execute(MagicMock(), env.id, "   ", _owner())

    repo.update_name.assert_not_called()


@pytest.mark.asyncio
async def test_rename_over_128_chars_raises_400():
    env = _env()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=env)
    repo.update_name = AsyncMock()
    uc = UpdateEnvironmentNameUseCase(repo)

    with pytest.raises(EnvironmentError, match="128"):
        await uc.execute(MagicMock(), env.id, "x" * 129, _owner())

    repo.update_name.assert_not_called()


@pytest.mark.asyncio
async def test_rename_wrong_owner_raises_403():
    env = _env(user_id=str(uuid4()))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=env)
    repo.update_name = AsyncMock()
    uc = UpdateEnvironmentNameUseCase(repo)

    with pytest.raises(BookingPermissionError):
        await uc.execute(MagicMock(), env.id, "new name", _other_user())

    repo.update_name.assert_not_called()


@pytest.mark.asyncio
async def test_admin_can_rename_any_environment():
    env = _env(user_id=str(uuid4()))
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[env, env])
    repo.update_name = AsyncMock()
    uc = UpdateEnvironmentNameUseCase(repo)

    await uc.execute(MagicMock(), env.id, "admin rename", _other_user(role="admin"))

    repo.update_name.assert_called_once()


# ── Route plumbing (JSON API + HTMX) ───────────────────────────────────────────

@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user
    user = make_fake_user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    yield TestClient(app), user
    app.dependency_overrides.clear()


def test_api_patch_booking_label_returns_200(user_client):
    client, _ = user_client
    booking = _booking(label="new label")
    with patch("app.presentation.routes.api_bookings._update_label_use_case") as uc:
        uc.execute = AsyncMock(return_value=booking)
        resp = client.patch(f"/api/bookings/{booking.id}", json={"label": "new label"})
    assert resp.status_code == 200
    assert resp.json()["label"] == "new label"


def test_api_patch_booking_label_not_found_404(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._update_label_use_case") as uc:
        uc.execute = AsyncMock(side_effect=BookingNotFoundError("nope"))
        resp = client.patch(f"/api/bookings/{uuid4()}", json={"label": "x"})
    assert resp.status_code == 404


def test_api_patch_booking_label_forbidden_403(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._update_label_use_case") as uc:
        uc.execute = AsyncMock(side_effect=BookingPermissionError("not allowed"))
        resp = client.patch(f"/api/bookings/{uuid4()}", json={"label": "x"})
    assert resp.status_code == 403


def test_api_patch_booking_label_too_long_400(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._update_label_use_case") as uc:
        uc.execute = AsyncMock(side_effect=BookingError("label must be at most 128 characters"))
        resp = client.patch(f"/api/bookings/{uuid4()}", json={"label": "x" * 200})
    assert resp.status_code == 400


def test_htmx_patch_booking_label_returns_html(user_client):
    client, _ = user_client
    booking = _booking(label="renamed")
    with patch("app.presentation.routes.bookings._update_label_use_case") as uc:
        uc.execute = AsyncMock(return_value=booking)
        resp = client.patch(f"/bookings/{booking.id}/label", data={"label": "renamed"})
    assert resp.status_code == 200
    assert "renamed" in resp.text


def test_api_patch_environment_name_returns_200(user_client):
    client, _ = user_client
    env = _env(name="renamed-stack")
    with patch("app.presentation.routes.api_environments._update_name_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.patch(f"/api/environments/{env.id}", json={"name": "renamed-stack"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed-stack"


def test_api_patch_environment_name_not_found_404(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_environments._update_name_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentNotFoundError("nope"))
        resp = client.patch(f"/api/environments/{uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


def test_api_patch_environment_name_blank_400(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_environments._update_name_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentError("name cannot be blank"))
        resp = client.patch(f"/api/environments/{uuid4()}", json={"name": "   "})
    assert resp.status_code == 400


def test_htmx_patch_environment_name_returns_html(user_client):
    client, _ = user_client
    env = _env(name="renamed-stack")
    with patch("app.presentation.routes.environments._update_name_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.patch(f"/environments/{env.id}/name", data={"name": "renamed-stack"})
    assert resp.status_code == 200
    assert "renamed-stack" in resp.text
