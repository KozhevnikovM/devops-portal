from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import StaticVMUnavailableError


def _make_static_booking(**kwargs) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=kwargs.get("id", uuid4()),
        user_id=kwargs.get("user_id", "dev-user"),
        status=kwargs.get("status", BookingStatus.READY),
        resource_type=ResourceType.STATIC_VM,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        static_vm_id=kwargs.get("static_vm_id", uuid4()),
        static_vm_name=kwargs.get("static_vm_name", "build-agent-1"),
        static_vm_host=kwargs.get("static_vm_host", "10.0.0.12"),
        static_vm_username=kwargs.get("static_vm_username", "ubuntu"),
        static_vm_password=kwargs.get("static_vm_password", "s3cret"),
        static_vm_ssh_key=kwargs.get("static_vm_ssh_key", None),
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
async def test_use_case_reserves_next_free_static_vm():
    from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase

    vm_id = uuid4()
    vm = SimpleNamespace(
        id=vm_id, name="build-agent-1", host="10.0.0.12",
        username="ubuntu", password="s3cret", ssh_key=None,
    )
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    svm_repo = MagicMock()
    svm_repo.lock_next_available = AsyncMock(return_value=vm)

    use_case = ReserveStaticVMUseCase(repo, svm_repo)
    booking = await use_case.execute(AsyncMock(), 240, user_id="u1")

    assert booking.status == BookingStatus.READY
    assert booking.resource_type == ResourceType.STATIC_VM
    assert booking.static_vm_id == vm_id
    assert booking.static_vm_host == "10.0.0.12"
    assert booking.static_vm_password == "s3cret"
    svm_repo.lock_next_available.assert_awaited_once()
    repo.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_case_reserves_specific_static_vm():
    from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase

    vm_id = uuid4()
    vm = SimpleNamespace(
        id=vm_id, name="build-agent-2", host="10.0.0.13",
        username="ubuntu", password="pw", ssh_key=None, is_active=True,
    )
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    svm_repo = MagicMock()
    svm_repo.lock_for_allocation = AsyncMock(return_value=vm)
    svm_repo.is_held = AsyncMock(return_value=False)
    svm_repo.lock_next_available = AsyncMock()

    use_case = ReserveStaticVMUseCase(repo, svm_repo)
    booking = await use_case.execute(AsyncMock(), 240, user_id="u1", static_vm_id=vm_id)

    assert booking.static_vm_id == vm_id
    assert booking.static_vm_name == "build-agent-2"
    svm_repo.lock_for_allocation.assert_awaited_once()
    svm_repo.lock_next_available.assert_not_called()  # specific pick, not auto-assign


@pytest.mark.asyncio
async def test_use_case_rejects_held_specific_static_vm():
    from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase

    vm = SimpleNamespace(id=uuid4(), name="busy", host="h", username="u", password="p", ssh_key=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock()
    svm_repo = MagicMock()
    svm_repo.lock_for_allocation = AsyncMock(return_value=vm)
    svm_repo.is_held = AsyncMock(return_value=True)

    use_case = ReserveStaticVMUseCase(repo, svm_repo)
    with pytest.raises(StaticVMUnavailableError):
        await use_case.execute(AsyncMock(), 240, user_id="u1", static_vm_id=vm.id)
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_use_case_queues_when_pool_empty():
    from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase

    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    svm_repo = MagicMock()
    svm_repo.lock_next_available = AsyncMock(return_value=None)

    use_case = ReserveStaticVMUseCase(repo, svm_repo)
    booking = await use_case.execute(AsyncMock(), 240, user_id="u1")

    # No free VM → enqueued, no resource assigned.
    assert booking.status == BookingStatus.QUEUED
    assert booking.resource_type == ResourceType.STATIC_VM
    assert booking.static_vm_id is None
    repo.create.assert_awaited_once()


# ── POST /bookings (static VM) ────────────────────────────────────────────────

def test_post_booking_static_vm_returns_row(client):
    cl, _ = client
    booking = _make_static_booking()
    with patch("app.presentation.routes.bookings._reserve_static_vm_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = cl.post("/bookings", data={"resource_type": "STATIC_VM", "ttl_minutes": "240"})

    assert resp.status_code == 201
    assert "build-agent-1" in resp.text
    assert "10.0.0.12" in resp.text
    assert "s3cret" in resp.text  # credentials shown to the owner


def test_post_booking_static_vm_json(client):
    cl, _ = client
    booking = _make_static_booking(static_vm_ssh_key="ssh-ed25519 AAAA")
    with patch("app.presentation.routes.api_bookings._reserve_static_vm_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = cl.post(
            "/api/bookings",
            json={"resource_type": "STATIC_VM", "ttl_minutes": 240},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["resource_type"] == "STATIC_VM"
    assert body["static_vm"] == "build-agent-1"
    assert body["host"] == "10.0.0.12"
    assert body["username"] == "ubuntu"
    assert body["password"] == "s3cret"
    assert body["ssh_key"] == "ssh-ed25519 AAAA"


def test_post_booking_static_vm_unavailable_409_json(client):
    cl, _ = client
    with patch("app.presentation.routes.api_bookings._reserve_static_vm_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(side_effect=StaticVMUnavailableError("No static VMs available"))
        resp = cl.post(
            "/api/bookings",
            json={"resource_type": "STATIC_VM", "ttl_minutes": 240},
        )

    assert resp.status_code == 409


def test_post_booking_static_vm_unavailable_html_shows_error(client):
    cl, _ = client
    with patch("app.presentation.routes.bookings._reserve_static_vm_use_case") as mock_uc, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_uc.execute = AsyncMock(side_effect=StaticVMUnavailableError("No static VMs available"))
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        resp = cl.post("/bookings", data={"resource_type": "STATIC_VM", "ttl_minutes": "240"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#booking-form-area"
    assert "No static VMs available" in resp.text


# ── Release (static VM) ───────────────────────────────────────────────────────

def test_release_static_vm_sets_released_without_teardown(client):
    cl, fake_user = client
    booking = _make_static_booking(user_id=str(fake_user.id), status=BookingStatus.READY)
    released = _make_static_booking(id=booking.id, user_id=str(fake_user.id), status=BookingStatus.RELEASED)

    from app.presentation.routes import api_bookings
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(side_effect=[booking, released])
    mock_repo.update_status = AsyncMock()
    mock_repo.promote_next_queued = AsyncMock()
    mock_dispatcher = MagicMock()
    with patch.object(api_bookings._release_use_case, "_repo", mock_repo), \
         patch.object(api_bookings._release_use_case, "_dispatcher", mock_dispatcher):
        resp = cl.delete(f"/api/bookings/{booking.id}")

    assert resp.status_code == 202
    assert resp.json()["status"] == "RELEASED"
    # status set directly to RELEASED; no teardown task queued for a pooled resource
    assert mock_repo.update_status.call_args.args[2] == BookingStatus.RELEASED
    mock_dispatcher.dispatch_teardown.assert_not_called()
    mock_repo.promote_next_queued.assert_awaited_once()


# ── Teardown task (static VM) ─────────────────────────────────────────────────

def test_teardown_task_static_vm_releases_without_adapter():
    from app.tasks import teardown as teardown_mod

    booking = _make_static_booking(status=BookingStatus.READY)
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


# ── Form rendering (Provisioned | Static) ─────────────────────────────────────

def test_vm_page_form_has_provisioned_static_toggle_and_dropdown(client):
    cl, _ = client
    vms = [
        SimpleNamespace(id=uuid4(), name="build-agent-1", host="10.0.0.12"),
        SimpleNamespace(id=uuid4(), name="build-agent-2", host="10.0.0.13"),
    ]
    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=vms)
        resp = cl.get("/")

    assert resp.status_code == 200
    assert 'name="resource_type" value="VM"' in resp.text
    assert 'name="resource_type" value="STATIC_VM"' in resp.text
    # "Any available" plus each specific VM in the dropdown.
    assert 'name="static_vm_id"' in resp.text
    assert "Any available (2)" in resp.text
    assert "build-agent-1 — 10.0.0.12" in resp.text


def test_post_booking_specific_static_vm_passes_id(client):
    cl, _ = client
    booking = _make_static_booking()
    with patch("app.presentation.routes.bookings._reserve_static_vm_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = cl.post(
            "/bookings",
            data={
                "resource_type": "STATIC_VM",
                "static_vm_id": str(booking.static_vm_id),
                "ttl_minutes": "240",
            },
        )

    assert resp.status_code == 201
    # the chosen id is forwarded to the use case
    assert mock_uc.execute.call_args.kwargs["static_vm_id"] == booking.static_vm_id


# ── Repository query semantics ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_next_available_uses_skip_locked():
    from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

    repo = StaticVMRepository()
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = lambda: None
    session.execute = AsyncMock(return_value=result)

    await repo.lock_next_available(session)
    stmt = session.execute.call_args.args[0]
    from sqlalchemy.dialects import postgresql
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE SKIP LOCKED" in compiled.upper()
    assert "is_active" in compiled
    # excludes static VMs held by a live booking
    assert "static_vm_id" in compiled
    assert "bookings" in compiled
