"""Regression test: environment child bookings must carry namespace_name (#271)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType

_OWNER_UUID = uuid4()
_OWNER_ID = str(_OWNER_UUID)


def _user():
    return User(id=_OWNER_UUID, username="alice", password_hash="", role="user",
                is_active=True, created_at=datetime.now(timezone.utc))


def _ns_booking(namespace_name: str):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=_OWNER_ID, status=BookingStatus.READY,
        resource_type=ResourceType.NAMESPACE, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
        namespace_name=namespace_name,
        cluster_name="prod",
    )


def _env_with_ns(namespace_name: str):
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=_OWNER_ID,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[_ns_booking(namespace_name)], owner_username="alice",
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    user = _user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_environment_row_shows_namespace_name(client):
    """The row template renders 'ns: <name>' when the environment holds a namespace."""
    env = _env_with_ns("my-namespace")
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/environments/{env.id}/row")
    assert resp.status_code == 200
    assert "ns: my-namespace" in resp.text


def test_environment_page_shows_namespace_name(client):
    """The environments list page also renders namespace names in the row."""
    env = _env_with_ns("team-ns")
    with patch("app.presentation.routes.environments._env_repo") as er, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        er.list_by_user = AsyncMock(return_value=[env])
        br.list_active = AsyncMock(return_value=[])
        nr.list_available = AsyncMock(return_value=[])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = client.get("/environments")
    assert resp.status_code == 200
    assert "ns: team-ns" in resp.text


def test_environment_row_no_namespace_name_hidden(client):
    """When the environment has no NAMESPACE child, the 'ns:' line is absent."""
    now = datetime.now(timezone.utc)
    vm_booking = Booking(
        id=uuid4(), user_id=_OWNER_ID, status=BookingStatus.READY,
        resource_type=ResourceType.VM, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now,
        image_name="Ubuntu",
    )
    env = Environment(
        id=uuid4(), name="vm-stack", blueprint_name="vm-only", user_id=_OWNER_ID,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[vm_booking], owner_username="alice",
    )
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/environments/{env.id}/row")
    assert resp.status_code == 200
    assert "ns:" not in resp.text
