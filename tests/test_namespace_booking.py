from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import NamespaceUnavailableError


def _make_ns_booking(**kwargs) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=kwargs.get("id", uuid4()),
        user_id=kwargs.get("user_id", "dev-user"),
        status=kwargs.get("status", BookingStatus.READY),
        resource_type=ResourceType.NAMESPACE,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        namespace_id=kwargs.get("namespace_id", uuid4()),
        namespace_name=kwargs.get("namespace_name", "team-a-dev"),
        cluster_name=kwargs.get("cluster_name", "prod-cluster"),
        api_url=kwargs.get("api_url", "https://api.cluster:6443"),
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


# ── Use case ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_use_case_allocates_free_namespace():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    ns_id = uuid4()
    ns_model = SimpleNamespace(id=ns_id, name="team-a-dev", cluster_name="prod", api_url=None, is_active=True)

    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    booking = await use_case.execute(AsyncMock(), ns_id, 240, user_id="u1")

    assert booking.status == BookingStatus.READY
    assert booking.resource_type == ResourceType.NAMESPACE
    assert booking.namespace_id == ns_id
    assert booking.namespace_name == "team-a-dev"
    ns_repo.lock_for_allocation.assert_awaited_once()
    repo.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_case_rejects_inactive_namespace():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    ns_model = SimpleNamespace(id=uuid4(), name="x", cluster_name="c", api_url=None, is_active=False)
    repo = MagicMock()
    repo.create = AsyncMock()
    ns_repo = MagicMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    with pytest.raises(NamespaceUnavailableError):
        await use_case.execute(AsyncMock(), ns_model.id, 240, user_id="u1")
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_use_case_rejects_held_namespace():
    from app.application.use_cases.book_namespace import BookNamespaceUseCase

    ns_model = SimpleNamespace(id=uuid4(), name="x", cluster_name="c", api_url=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock()
    ns_repo = MagicMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=True)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    with pytest.raises(NamespaceUnavailableError):
        await use_case.execute(AsyncMock(), ns_model.id, 240, user_id="u1")
    repo.create.assert_not_called()


# ── POST /bookings (namespace) ────────────────────────────────────────────────

def test_post_booking_namespace_returns_row(client):
    cl, _ = client
    booking = _make_ns_booking()
    with patch("app.presentation.routes.bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = cl.post(
            "/bookings",
            data={"resource_type": "NAMESPACE", "namespace_id": str(booking.namespace_id), "ttl_minutes": "240"},
        )

    assert resp.status_code == 201
    assert "team-a-dev" in resp.text
    assert "prod-cluster" in resp.text


def test_post_booking_namespace_json(client):
    cl, _ = client
    booking = _make_ns_booking()
    with patch("app.presentation.routes.bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = cl.post(
            "/bookings",
            data={"resource_type": "NAMESPACE", "namespace_id": str(booking.namespace_id), "ttl_minutes": "240"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["resource_type"] == "NAMESPACE"
    assert body["namespace"] == "team-a-dev"
    assert body["cluster"] == "prod-cluster"
    assert body["api_url"] == "https://api.cluster:6443"


def test_post_booking_namespace_unavailable_409_json(client):
    cl, _ = client
    with patch("app.presentation.routes.bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(side_effect=NamespaceUnavailableError("already booked"))
        resp = cl.post(
            "/bookings",
            data={"resource_type": "NAMESPACE", "namespace_id": str(uuid4()), "ttl_minutes": "240"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 409


def test_post_booking_namespace_missing_id_html_shows_error(client):
    cl, _ = client
    with patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        resp = cl.post("/bookings", data={"resource_type": "NAMESPACE", "ttl_minutes": "240"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#booking-form-area"
    assert "Select a namespace" in resp.text


# ── Release (namespace) ───────────────────────────────────────────────────────

def test_release_namespace_sets_released_without_teardown(client):
    cl, fake_user = client
    booking = _make_ns_booking(user_id=str(fake_user.id), status=BookingStatus.READY)
    released = _make_ns_booking(id=booking.id, user_id=str(fake_user.id), status=BookingStatus.RELEASED)

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.tasks.teardown.teardown_vm_task") as mock_task:
        mock_repo.get = AsyncMock(side_effect=[booking, released])
        mock_repo.update_status = AsyncMock()
        resp = cl.delete(f"/bookings/{booking.id}", headers={"Accept": "application/json"})

    assert resp.status_code == 202
    assert resp.json()["status"] == "RELEASED"
    # status set directly to RELEASED; no teardown task queued for a namespace
    assert mock_repo.update_status.call_args.args[2] == BookingStatus.RELEASED
    mock_task.delay.assert_not_called()


# ── Teardown task (namespace) ─────────────────────────────────────────────────

def test_teardown_task_namespace_releases_without_adapter():
    from app.tasks import teardown as teardown_mod

    booking = _make_ns_booking(status=BookingStatus.READY)
    with patch.object(teardown_mod, "repo") as mock_repo, \
         patch.object(teardown_mod, "terraform") as mock_tf, \
         patch.object(teardown_mod, "SyncSessionLocal") as mock_session_local:
        mock_session_local.return_value.__enter__.return_value = MagicMock()
        mock_repo.sync_get = MagicMock(return_value=booking)
        mock_repo.sync_update_status = MagicMock()

        teardown_mod.teardown_vm_task.run(str(booking.id))

    mock_repo.sync_update_status.assert_called_once()
    assert mock_repo.sync_update_status.call_args.args[2] == BookingStatus.RELEASED
    mock_tf.destroy.assert_not_called()


# ── Booking form rendering ────────────────────────────────────────────────────

def test_namespace_page_renders_namespace_form(client):
    cl, _ = client
    ns = SimpleNamespace(id=uuid4(), name="team-a-dev", cluster_name="prod-cluster")
    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[ns])
        mock_svm.count_available = AsyncMock(return_value=0)
        resp = cl.get("/book/namespace")

    assert resp.status_code == 200
    # The namespace page submits a hidden NAMESPACE resource_type and offers the dropdown.
    assert 'name="resource_type" value="NAMESPACE"' in resp.text
    assert "team-a-dev (prod-cluster)" in resp.text
    # VM-only fields are not on the namespace page.
    assert 'name="image_id"' not in resp.text
    # Lists only namespace bookings.
    assert mock_repo.list_by_user.call_args.kwargs["resource_type"] == "NAMESPACE"


def test_vm_page_lists_only_vm_bookings(client):
    cl, _ = client
    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.count_available = AsyncMock(return_value=0)
        resp = cl.get("/")

    assert resp.status_code == 200
    assert 'name="image_id"' in resp.text
    # VM page lists both provisioned and static VMs.
    assert mock_repo.list_by_user.call_args.kwargs["resource_type"] == ["VM", "STATIC_VM"]


def test_action_menu_not_clipped_by_table_wrapper(client):
    """Regression: the bookings table wrapper must not clip the row's ⋮ dropdown."""
    cl, fake_user = client
    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid4(),
        user_id=str(fake_user.id),
        status=BookingStatus.READY,
        resource_type=ResourceType.VM,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip="10.0.0.1",
        owner_username=fake_user.username,
    )
    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[booking])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.count_available = AsyncMock(return_value=0)
        resp = cl.get("/")

    assert resp.status_code == 200
    # The ⋮ dropdown layer is rendered …
    assert "z-50" in resp.text
    # … and the table wrapper no longer clips it.
    assert "rounded-lg overflow-hidden" not in resp.text
    # … and a click-outside handler closes open dropdowns.
    assert "details[open]" in resp.text


def test_header_nav_shows_booking_types(client):
    cl, _ = client
    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.count_available = AsyncMock(return_value=0)
        resp = cl.get("/")

    assert resp.status_code == 200
    assert 'href="/book/vm"' in resp.text
    assert 'href="/book/namespace"' in resp.text
    assert "Environment" in resp.text
    assert "cursor-not-allowed" in resp.text  # Environment is disabled
