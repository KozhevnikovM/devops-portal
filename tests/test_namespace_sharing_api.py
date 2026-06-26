"""API route tests for namespace sharing endpoints.

Uses TestClient with dependency overrides (no real DB/Celery/Redis needed).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, NamespaceShare, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingNotFoundError,
    BookingPermissionError,
    NamespaceShareDuplicateError,
    NamespaceShareNotFoundError,
    NamespaceShareSelfError,
    NamespaceShareUserNotFoundError,
)

_NOW = datetime.now(timezone.utc)
_OWNER_ID = uuid4()
_OTHER_ID = uuid4()
_BOOKING_ID = uuid4()


def _owner_user():
    return User(id=_OWNER_ID, username="bob", password_hash="", role="user",
                is_active=True, created_at=_NOW)


def _ns_booking(status=BookingStatus.READY):
    return Booking(
        id=_BOOKING_ID, user_id=str(_OWNER_ID), status=status,
        resource_type=ResourceType.NAMESPACE,
        ttl_minutes=120, expires_at=_NOW, created_at=_NOW,
        namespace_name="dev1", cluster_name="prod", api_url="https://api.cluster:6443",
        owner_username="bob",
    )


def _share(username="alice"):
    return NamespaceShare(
        id=uuid4(), booking_id=_BOOKING_ID,
        shared_with_user_id=_OTHER_ID, shared_with_username=username,
        created_at=_NOW,
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = _owner_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    admin = User(id=uuid4(), username="admin", password_hash="", role="admin",
                 is_active=True, created_at=_NOW)
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: admin
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── POST /api/bookings/{id}/shares ───────────────────────────────────────────

def test_create_share_201(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(return_value=_share("alice"))
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "alice"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["shared_with"] == "alice"
    assert body["booking_id"] == str(_BOOKING_ID)
    assert "created_at" in body


def test_create_share_non_owner_403(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(side_effect=BookingPermissionError("not authorized"))
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "alice"},
        )
    assert resp.status_code == 403


def test_create_share_unknown_user_400(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(
            side_effect=NamespaceShareUserNotFoundError("User 'ghost' not found or inactive")
        )
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "ghost"},
        )
    assert resp.status_code == 400


def test_create_share_self_400(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(
            side_effect=NamespaceShareSelfError("Cannot share with yourself")
        )
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "bob"},
        )
    assert resp.status_code == 400


def test_create_share_duplicate_409(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(
            side_effect=NamespaceShareDuplicateError("already shared")
        )
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "alice"},
        )
    assert resp.status_code == 409


def test_create_share_booking_not_found_404(client):
    with patch("app.presentation.routes.api_namespaces._share_uc") as uc:
        uc.execute = AsyncMock(side_effect=BookingNotFoundError("not found"))
        resp = client.post(
            f"/api/bookings/{_BOOKING_ID}/shares",
            json={"username": "alice"},
        )
    assert resp.status_code == 404


# ── GET /api/bookings/{id}/shares ────────────────────────────────────────────

def test_list_shares_200(client):
    booking = _ns_booking()
    shares = [_share("alice"), _share("carol")]
    # The route loads the booking to check can_manage, then lists shares.
    with patch("app.presentation.routes.api_namespaces._booking_repo") as repo, \
         patch("app.presentation.routes.api_namespaces._share_repo") as sr:
        repo.get = AsyncMock(return_value=booking)
        sr.list_by_booking = AsyncMock(return_value=shares)
        resp = client.get(f"/api/bookings/{_BOOKING_ID}/shares")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["username"] == "alice"


def test_list_shares_booking_not_found_404(client):
    with patch("app.presentation.routes.api_namespaces._booking_repo") as repo:
        repo.get = AsyncMock(side_effect=ValueError("not found"))
        resp = client.get(f"/api/bookings/{_BOOKING_ID}/shares")
    assert resp.status_code == 404


def test_list_shares_non_owner_403(client):
    # Booking owned by someone else.
    booking = Booking(
        id=_BOOKING_ID, user_id="someone-else", status=BookingStatus.READY,
        resource_type=ResourceType.NAMESPACE, ttl_minutes=120, expires_at=_NOW, created_at=_NOW,
    )
    with patch("app.presentation.routes.api_namespaces._booking_repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        resp = client.get(f"/api/bookings/{_BOOKING_ID}/shares")
    assert resp.status_code == 403


# ── DELETE /api/bookings/{id}/shares/{username} ──────────────────────────────

def test_revoke_share_204(client):
    with patch("app.presentation.routes.api_namespaces._revoke_uc") as uc:
        uc.execute = AsyncMock(return_value=None)
        resp = client.delete(f"/api/bookings/{_BOOKING_ID}/shares/alice")
    assert resp.status_code == 204


def test_revoke_share_non_owner_403(client):
    with patch("app.presentation.routes.api_namespaces._revoke_uc") as uc:
        uc.execute = AsyncMock(side_effect=BookingPermissionError("not authorized"))
        resp = client.delete(f"/api/bookings/{_BOOKING_ID}/shares/alice")
    assert resp.status_code == 403


def test_revoke_share_not_found_404(client):
    with patch("app.presentation.routes.api_namespaces._revoke_uc") as uc:
        uc.execute = AsyncMock(side_effect=NamespaceShareNotFoundError("not found"))
        resp = client.delete(f"/api/bookings/{_BOOKING_ID}/shares/alice")
    assert resp.status_code == 404


def test_revoke_share_user_not_found_404(client):
    with patch("app.presentation.routes.api_namespaces._revoke_uc") as uc:
        uc.execute = AsyncMock(side_effect=NamespaceShareUserNotFoundError("ghost not found"))
        resp = client.delete(f"/api/bookings/{_BOOKING_ID}/shares/ghost")
    assert resp.status_code == 404


# ── GET /api/namespaces/shared-with-me ───────────────────────────────────────

def test_shared_with_me_200(client):
    entries = [
        {
            "booking_id": str(_BOOKING_ID),
            "status": "READY",
            "namespace": "dev1",
            "cluster": "prod",
            "api_url": "https://api.cluster:6443",
            "owner_username": "bob",
            "environment": None,
        }
    ]
    with patch("app.presentation.routes.api_namespaces._share_repo") as sr:
        sr.list_shared_with_user = AsyncMock(return_value=entries)
        resp = client.get("/api/namespaces/shared-with-me")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["namespace"] == "dev1"


def test_shared_with_me_empty(client):
    with patch("app.presentation.routes.api_namespaces._share_repo") as sr:
        sr.list_shared_with_user = AsyncMock(return_value=[])
        resp = client.get("/api/namespaces/shared-with-me")
    assert resp.status_code == 200
    assert resp.json() == []


def test_shared_with_me_with_environment(client):
    entries = [
        {
            "booking_id": str(_BOOKING_ID),
            "status": "READY",
            "namespace": "dev1",
            "cluster": "prod",
            "api_url": None,
            "owner_username": "bob",
            "environment": {"id": str(uuid4()), "name": "dev-stack"},
        }
    ]
    with patch("app.presentation.routes.api_namespaces._share_repo") as sr:
        sr.list_shared_with_user = AsyncMock(return_value=entries)
        resp = client.get("/api/namespaces/shared-with-me")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["environment"]["name"] == "dev-stack"
