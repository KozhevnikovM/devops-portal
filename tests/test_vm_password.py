"""Tests for VM connection password generation and display (feature #83)."""
import string
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus


def _make_booking(
    status: BookingStatus = BookingStatus.READY,
    vm_ip: str | None = "10.0.0.1",
    vm_password: str | None = "Abc123XyZ456qwER",
    owner_username: str | None = "alice",
    user_id: str = "user-alice",
) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=status,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip=vm_ip,
        vm_password=vm_password,
        owner_username=owner_username,
    )


def _make_user(username: str = "alice", role: str = "user") -> User:
    return User(
        id=uuid4(),
        username=username,
        password_hash="",
        role=role,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client_as_owner():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    session_mock = AsyncMock()
    owner = _make_user("alice", "user")
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: owner
    yield TestClient(app), owner
    app.dependency_overrides.clear()


@pytest.fixture
def client_as_admin():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    session_mock = AsyncMock()
    admin = _make_user("admin", "admin")
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: admin
    yield TestClient(app), admin
    app.dependency_overrides.clear()


@pytest.fixture
def client_as_other():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    session_mock = AsyncMock()
    other = _make_user("bob", "user")
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: other
    yield TestClient(app), other
    app.dependency_overrides.clear()


# --- Provision task tests ---

def test_provision_task_stores_password_on_ready():
    """provision_vm_task must pass a vm_password kwarg when setting READY."""
    from app.domain.entities import VMImage, HWConfig

    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())
    now = datetime.now(timezone.utc)

    fake_image = VMImage(id=image_id, name="Ubuntu", vapp_template_id="tpl-1",
                         is_active=True, created_at=now)
    fake_hw = HWConfig(id=hw_config_id, name="medium", cpus=2, memory_mb=4096,
                       disk_mb=26624, is_active=True, created_at=now)

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=fake_image)
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=fake_hw)

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    calls = mock_repo.sync_update_status.call_args_list
    ready_call = next(c for c in calls if c.args[2] == BookingStatus.READY)
    assert ready_call.kwargs.get("vm_password") is not None


def test_provision_task_password_is_16_alphanumeric():
    """Generated password is 16 chars drawn from letters+digits only."""
    from app.domain.entities import VMImage, HWConfig

    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())
    now = datetime.now(timezone.utc)

    fake_image = VMImage(id=image_id, name="Ubuntu", vapp_template_id="tpl-1",
                         is_active=True, created_at=now)
    fake_hw = HWConfig(id=hw_config_id, name="medium", cpus=2, memory_mb=4096,
                       disk_mb=26624, is_active=True, created_at=now)

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(return_value=fake_image)
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(return_value=fake_hw)

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    calls = mock_repo.sync_update_status.call_args_list
    ready_call = next(c for c in calls if c.args[2] == BookingStatus.READY)
    pw = ready_call.kwargs["vm_password"]
    assert len(pw) == 16
    allowed = set(string.ascii_letters + string.digits)
    assert all(c in allowed for c in pw)


# --- Booking row UI tests ---

def test_booking_row_shows_password_to_owner(client_as_owner):
    client, owner = client_as_owner
    booking = _make_booking(owner_username=owner.username, user_id=str(owner.id))
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/row")

    assert resp.status_code == 200
    assert "Abc123XyZ456qwER" in resp.text


def test_booking_row_shows_password_to_admin(client_as_admin):
    client, admin = client_as_admin
    booking = _make_booking(owner_username="alice")
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/row")

    assert resp.status_code == 200
    assert "Abc123XyZ456qwER" in resp.text


def test_booking_row_denies_other_user(client_as_other):
    """#138: a non-owner is rejected outright (403) — no row, no leaked detail."""
    client, other = client_as_other
    booking = _make_booking(owner_username="alice")
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/row")

    assert resp.status_code == 403
    assert "Abc123XyZ456qwER" not in resp.text
    assert "10.0.0.1" not in resp.text


def test_booking_row_no_password_when_not_ready(client_as_owner):
    client, owner = client_as_owner
    booking = _make_booking(
        status=BookingStatus.PROVISIONING,
        vm_ip=None,
        vm_password=None,
        owner_username=owner.username,
        user_id=str(owner.id),
    )
    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/bookings/{booking.id}/row")

    assert resp.status_code == 200
    assert "Abc123XyZ456qwER" not in resp.text


# --- JSON API test ---

def test_list_bookings_json_omits_vm_password(client_as_admin):
    """#137: the list endpoint must never vend secrets, even to an admin.

    The password is delivered only via the creation response and the
    owner/admin-gated single-row view.
    """
    client, _ = client_as_admin
    booking = _make_booking()
    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=[booking])
        resp = client.get("/api/bookings")

    assert resp.status_code == 200
    row = resp.json()[0]
    assert "vm_password" not in row
    assert "Abc123XyZ456qwER" not in resp.text
