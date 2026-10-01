"""#478 — booking credentials are revealed only by an explicit, owner-or-admin request.

`GET /bookings/{id}/credentials` is the one place credential values leave the server for the browser
UI. Every row-rendering path (bookings page, /row refresh, action responses, SSE) shows at most a
"Show credentials" control, never the values themselves.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, User
from app.domain.pagination import KeysetPage
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError

VM_PASSWORD = "vm-Pa55-0f7e3c"
SVM_PASSWORD = "svm-Pa55-91ab2d"
SVM_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5-secret-key-4f1e"
SECRETS = (VM_PASSWORD, SVM_PASSWORD, SVM_KEY)

_ROUTES = "app.presentation.routes.bookings"


def _user(role: str = "user", username: str = "someone") -> User:
    return User(
        id=uuid4(), username=username, password_hash="", role=role, is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _vm(owner: User | str, *, status=BookingStatus.READY, created_by=None, **kw) -> Booking:
    now = datetime.now(timezone.utc)
    owner_id = str(owner.id) if isinstance(owner, User) else owner
    owner_name = owner.username if isinstance(owner, User) else "alice"
    fields = {
        "id": uuid4(), "user_id": owner_id, "status": status, "ttl_minutes": 240,
        "expires_at": now + timedelta(minutes=240), "created_at": now,
        "image_id": uuid4(), "image_name": "Ubuntu 22.04",
        "hw_config_id": uuid4(), "hw_config_name": "medium",
        "vm_ip": "10.0.0.1", "vm_password": VM_PASSWORD, "owner_username": owner_name,
        "created_by": created_by,
    }
    fields.update(kw)
    return Booking(**fields)


def _static_vm(owner: User | str, **kw) -> Booking:
    return _vm(
        owner, resource_type=ResourceType.STATIC_VM, image_id=None, image_name=None,
        hw_config_id=None, hw_config_name=None, vm_ip=None, vm_password=None,
        static_vm_name="build-agent-1", static_vm_host="10.0.0.12", static_vm_username="ubuntu",
        static_vm_password=SVM_PASSWORD, static_vm_ssh_key=SVM_KEY, **kw,
    )


def _client(user: User | None) -> TestClient:
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    if user is not None:
        app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _get_credentials(user: User | None, booking: Booking):
    with patch(f"{_ROUTES}._repo") as repo:
        repo.get = AsyncMock(return_value=booking)
        return _client(user).get(
            f"/bookings/{booking.id}/credentials", follow_redirects=False,
        )


def _assert_no_secret(text: str) -> None:
    leaked = [s for s in SECRETS if s in text]
    assert leaked == [], f"credential values leaked: {leaked}"


def _control(booking: Booking) -> str:
    return f'hx-get="/bookings/{booking.id}/credentials"'


# ── 4.2 the endpoint's permission matrix ────────────────────────────────────────

def test_owner_gets_vm_password_uncached_and_out_of_htmx_history():
    owner = _user()
    resp = _get_credentials(owner, _vm(owner))

    assert resp.status_code == 200
    assert VM_PASSWORD in resp.text
    assert resp.headers["cache-control"] == "no-store"
    # Design D6: while the fragment is on the page htmx must not snapshot it into localStorage.
    assert resp.text.lstrip().startswith("<div")
    assert 'hx-history="false"' in resp.text.split(">", 1)[0]


def test_owner_gets_static_vm_credentials():
    owner = _user()
    resp = _get_credentials(owner, _static_vm(owner))

    assert resp.status_code == 200
    assert "ubuntu" in resp.text
    assert SVM_PASSWORD in resp.text
    assert SVM_KEY in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert 'hx-history="false"' in resp.text


def test_admin_gets_another_users_credentials():
    resp = _get_credentials(_user("admin", "root"), _vm("someone-else"))

    assert resp.status_code == 200
    assert VM_PASSWORD in resp.text


def test_creating_dispatcher_is_refused():
    """The dispatcher may manage the booking (can_manage) but never see its secrets."""
    dispatcher = _user("dispatcher", "disp")
    resp = _get_credentials(dispatcher, _vm("someone-else", created_by=str(dispatcher.id)))

    assert resp.status_code == 403
    _assert_no_secret(resp.text)


def test_unrelated_user_is_refused():
    resp = _get_credentials(_user(), _static_vm("someone-else"))

    assert resp.status_code == 403
    _assert_no_secret(resp.text)


def test_unauthenticated_caller_is_refused():
    resp = _get_credentials(None, _vm("someone-else"))

    assert resp.status_code in (302, 401)
    _assert_no_secret(resp.text)


def test_unknown_booking_is_404():
    with patch(f"{_ROUTES}._repo") as repo:
        repo.get = AsyncMock(side_effect=BookingNotFoundError("nope"))
        resp = _client(_user()).get(f"/bookings/{uuid4()}/credentials")

    assert resp.status_code == 404


@pytest.mark.parametrize("status", [BookingStatus.RELEASED, BookingStatus.PROVISIONING])
def test_owner_gets_409_when_not_ready(status):
    owner = _user()
    resp = _get_credentials(owner, _vm(owner, status=status))

    assert resp.status_code == 409
    _assert_no_secret(resp.text)


def test_unrelated_user_gets_403_not_409_for_non_ready_booking():
    """Authorization before status: a refused caller cannot learn the booking's status."""
    resp = _get_credentials(_user(), _vm("someone-else", status=BookingStatus.RELEASED))

    assert resp.status_code == 403


# ── 4.3 no row-rendering path embeds a credential value ─────────────────────────

def _render_page(user: User, bookings: list[Booking], *, filter: str = "mine") -> str:
    with patch(f"{_ROUTES}._repo") as repo, \
         patch(f"{_ROUTES}._image_repo") as img, \
         patch(f"{_ROUTES}._hw_config_repo") as hw, \
         patch(f"{_ROUTES}._namespace_repo") as ns, \
         patch(f"{_ROUTES}._static_vm_repo") as svm, \
         patch(f"{_ROUTES}._role_repo") as role:
        repo.list_page = AsyncMock(return_value=KeysetPage(items=bookings))
        repo.queue_positions = AsyncMock(return_value={})
        for r in (img, hw, role):
            r.list_active = AsyncMock(return_value=[])
        for r in (ns, svm):
            r.list_available = AsyncMock(return_value=[])
        resp = _client(user).get(f"/book/vm?filter={filter}")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("filter", ["mine", "all"])
def test_bookings_page_offers_control_to_owner_but_embeds_no_secret(filter):
    owner = _user()
    vm, svm = _vm(owner), _static_vm(owner)
    html = _render_page(owner, [vm, svm], filter=filter)

    _assert_no_secret(html)
    assert _control(vm) in html
    assert _control(svm) in html
    # Nothing revealed yet, so ordinary page visits stay in the htmx history cache.
    assert 'hx-history="false"' not in html


def test_bookings_page_offers_control_to_admin():
    vm = _vm("someone-else")
    html = _render_page(_user("admin", "root"), [vm], filter="all")

    _assert_no_secret(html)
    assert _control(vm) in html


def test_bookings_page_offers_no_control_to_non_admin_viewer_of_foreign_booking():
    vm = _vm("someone-else")
    html = _render_page(_user(), [vm], filter="all")

    _assert_no_secret(html)
    assert _control(vm) not in html


def test_bookings_page_offers_no_control_to_creating_dispatcher():
    dispatcher = _user("dispatcher", "disp")
    vm = _vm("someone-else", created_by=str(dispatcher.id))
    html = _render_page(dispatcher, [vm], filter="mine")

    _assert_no_secret(html)
    assert _control(vm) not in html


def test_no_control_when_booking_has_no_credentials():
    owner = _user()
    vm = _vm(owner, vm_password=None)
    html = _render_page(owner, [vm])

    assert _control(vm) not in html


def test_row_refresh_embeds_no_secret():
    owner = _user()
    svm = _static_vm(owner)
    with patch(f"{_ROUTES}._repo") as repo:
        repo.get = AsyncMock(return_value=svm)
        repo.queue_positions = AsyncMock(return_value={})
        resp = _client(owner).get(f"/bookings/{svm.id}/row")

    assert resp.status_code == 200
    _assert_no_secret(resp.text)
    assert _control(svm) in resp.text


def test_label_change_response_embeds_no_secret():
    owner = _user()
    vm = _vm(owner, label="renamed")
    with patch(f"{_ROUTES}._update_label_use_case") as uc:
        uc.execute = AsyncMock(return_value=vm)
        resp = _client(owner).patch(f"/bookings/{vm.id}/label", data={"label": "renamed"})

    assert resp.status_code == 200
    _assert_no_secret(resp.text)
    assert _control(vm) in resp.text


@pytest.mark.asyncio
async def test_live_row_update_embeds_no_secret():
    from app.presentation.routes import events

    owner = _user()
    svm = _static_vm(owner)

    @asynccontextmanager
    async def _session():
        yield MagicMock()

    with patch.object(events, "AsyncSessionLocal", _session), \
         patch.object(events, "_booking_repo") as repo:
        repo.get = AsyncMock(return_value=svm)
        event = await events._render_booking_event(owner, str(svm.id))

    assert event is not None
    _assert_no_secret(event)
    assert _control(svm) in event
