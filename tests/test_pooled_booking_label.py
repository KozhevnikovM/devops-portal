"""Tests for #345: namespace/static-VM bookings can carry an optional label, matching
what VM bookings already support."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType


def _ns_booking(**kwargs) -> Booking:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid4(), user_id="u", status=BookingStatus.READY, resource_type=ResourceType.NAMESPACE,
        ttl_minutes=60, expires_at=now + timedelta(hours=1), created_at=now,
        namespace_id=uuid4(), namespace_name="team-a-dev", cluster_name="prod", api_url=None,
    )
    defaults.update(kwargs)
    return Booking(**defaults)


def _static_vm_booking(**kwargs) -> Booking:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid4(), user_id="u", status=BookingStatus.READY, resource_type=ResourceType.STATIC_VM,
        ttl_minutes=60, expires_at=now + timedelta(hours=1), created_at=now,
        static_vm_id=uuid4(), static_vm_name="svm-1", static_vm_host="10.0.0.5",
        static_vm_username="admin", static_vm_password="pw", static_vm_ssh_key=None,
    )
    defaults.update(kwargs)
    return Booking(**defaults)


# ── ReservePooledResourceUseCase (shared base) ─────────────────────────────────

@pytest.mark.asyncio
async def test_namespace_reservation_persists_label():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    ns_id = uuid4()
    ns_model = SimpleNamespace(id=ns_id, name="team-a-dev", cluster_name="prod", api_url=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    booking = await use_case.execute(
        AsyncMock(), 240, user_id="u1", namespace_id=ns_id, label="my perf test"
    )

    assert booking.label == "my perf test"


@pytest.mark.asyncio
async def test_namespace_queued_booking_persists_label():
    """Pool exhausted → enqueued; the label must survive on the QUEUED booking too."""
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.lock_next_available = AsyncMock(return_value=None)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    booking = await use_case.execute(AsyncMock(), 240, user_id="u1", label="queued label")

    assert booking.status == BookingStatus.QUEUED
    assert booking.label == "queued label"


@pytest.mark.asyncio
async def test_static_vm_reservation_persists_label():
    """Static VMs share ReservePooledResourceUseCase with namespaces — same label support."""
    from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase

    vm_id = uuid4()
    vm_model = SimpleNamespace(
        id=vm_id, name="svm-1", host="10.0.0.5", username="admin", password="pw",
        ssh_key=None, is_active=True,
    )
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    vm_repo = MagicMock()
    vm_repo.lock_for_allocation = AsyncMock(return_value=vm_model)
    vm_repo.is_held = AsyncMock(return_value=False)

    use_case = ReserveStaticVMUseCase(repo, vm_repo)
    booking = await use_case.execute(
        AsyncMock(), 240, user_id="u1", static_vm_id=vm_id, label="static label"
    )

    assert booking.label == "static label"


@pytest.mark.asyncio
async def test_namespace_reservation_no_label_defaults_none():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    ns_id = uuid4()
    ns_model = SimpleNamespace(id=ns_id, name="ns", cluster_name="c", api_url=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    booking = await use_case.execute(AsyncMock(), 240, user_id="u1", namespace_id=ns_id)

    assert booking.label is None


# ── Route plumbing (HTMX + JSON API) ───────────────────────────────────────────

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


def test_htmx_namespace_booking_forwards_label(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.bookings._book_namespace_use_case") as uc:
        uc.execute = AsyncMock(return_value=_ns_booking())
        resp = client.post("/bookings", data={
            "ttl_minutes": "60", "resource_type": "NAMESPACE", "label": "  ns label  ",
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["label"] == "ns label"


def test_htmx_static_vm_booking_forwards_label(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.bookings._reserve_static_vm_use_case") as uc:
        uc.execute = AsyncMock(return_value=_static_vm_booking())
        resp = client.post("/bookings", data={
            "ttl_minutes": "60", "resource_type": "STATIC_VM", "label": "svm label",
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["label"] == "svm label"


def test_api_namespace_booking_forwards_label(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._book_namespace_use_case") as uc:
        uc.execute = AsyncMock(return_value=_ns_booking())
        resp = client.post("/api/bookings", json={
            "resource_type": "NAMESPACE", "ttl_minutes": 60, "label": "api ns label",
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["label"] == "api ns label"


def test_api_static_vm_booking_forwards_label(user_client):
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._reserve_static_vm_use_case") as uc:
        uc.execute = AsyncMock(return_value=_static_vm_booking())
        resp = client.post("/api/bookings", json={
            "resource_type": "STATIC_VM", "ttl_minutes": 60, "label": "api svm label",
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["label"] == "api svm label"


def test_api_namespace_booking_label_truncated(user_client):
    """Same [:128].strip() rule the VM branch already applies (#107)."""
    client, _ = user_client
    with patch("app.presentation.routes.api_bookings._book_namespace_use_case") as uc:
        uc.execute = AsyncMock(return_value=_ns_booking())
        resp = client.post("/api/bookings", json={
            "resource_type": "NAMESPACE", "ttl_minutes": 60, "label": "x" * 200,
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["label"] == "x" * 128
