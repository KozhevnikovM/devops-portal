"""Regression test: first row's "⋮" actions menu opens downward, not upward.

See docs/bugfix/booking-row-first-actions-menu-hidden.md. `bottom-full` needs open space
above the trigger; the first row in the table has no row above it, so on common viewport
heights the top of the menu renders off-screen and unreachable.

The template previously guarded this with `{% if loop is defined and loop.first %}` —
but Jinja's `{% include %}` never propagates the special `loop` object into the included
template (unlike ordinary context/`{% set %}` variables), so that guard silently never
fired: `loop is defined` was always False inside `booking_row.html`/`environment_row.html`,
even for the genuine first-row list render, and every row kept rendering `bottom-full`.
The real fix sets an explicit `is_first_row = loop.first` in the parent template
(`index.html`/`environments.html`) right before the `{% include %}`, which *does*
propagate, and the partials check `is_first_row` instead of `loop`. Every standalone
render (booking creation, polling, extend, label-update, release; same for environments)
never sets `is_first_row`, so `is_first_row is defined` is False there and behavior stays
`bottom-full`, unchanged — exactly as it was meant to for the `loop`-based guard, just now
actually working for the list-render case too.
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.domain.entities import Booking, Environment
from app.domain.enums import BookingStatus, ResourceType

# w-48: booking_row's/environment_row's own menu width, distinct from base.html's header
# dropdown (w-44).
_MENU_RE = re.compile(r'class="absolute right-0 (top-full mt-1|bottom-full mb-1) w-48')


def _make_booking(user_id: str, owner_username: str) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
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
        owner_username=owner_username,
    )


def _client(fake_user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    return app, session_mock


def test_first_row_opens_downward_others_upward():
    from tests.conftest import make_fake_user

    fake_user = make_fake_user()
    app, _ = _client(fake_user)
    from fastapi.testclient import TestClient

    b1 = _make_booking(str(fake_user.id), fake_user.username)
    b2 = _make_booking(str(fake_user.id), fake_user.username)

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role:
        mock_repo.list_by_user = AsyncMock(return_value=[b1, b2])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])
        resp = TestClient(app).get("/")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    matches = _MENU_RE.findall(resp.text)
    assert len(matches) == 2, f"expected exactly 2 action menus, found {matches}"
    assert matches[0] == "top-full mt-1"
    assert matches[1] == "bottom-full mb-1"


def test_standalone_row_poll_keeps_bottom_full_no_500():
    """GET /bookings/{id}/row has no loop context; must not 500 and must keep bottom-full."""
    from tests.conftest import make_fake_user

    fake_user = make_fake_user()
    app, _ = _client(fake_user)
    from fastapi.testclient import TestClient

    booking = _make_booking(str(fake_user.id), fake_user.username)

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        resp = TestClient(app).get(f"/bookings/{booking.id}/row")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    matches = _MENU_RE.findall(resp.text)
    assert matches == ["bottom-full mb-1"]


# ── Environments page — identical bug/fix, same include structure ────────────

def _make_environment(owner_username: str) -> Environment:
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id=uuid4(),
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[], owner_username=owner_username,
    )


def test_first_environment_row_opens_downward_others_upward():
    from tests.conftest import make_fake_admin
    from fastapi.testclient import TestClient

    admin = make_fake_admin()
    app, _ = _client(admin)

    e1 = _make_environment(admin.username)
    e2 = _make_environment(admin.username)

    with patch("app.presentation.routes.environments._env_repo") as mock_er, \
         patch("app.presentation.routes.environments._blueprint_repo") as mock_br, \
         patch("app.presentation.routes.environments._namespace_repo") as mock_ns:
        mock_er.list_by_user = AsyncMock(return_value=[e1, e2])
        mock_br.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_ns.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = TestClient(app).get("/environments")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    matches = _MENU_RE.findall(resp.text)
    assert len(matches) == 2, f"expected exactly 2 action menus, found {matches}"
    assert matches[0] == "top-full mt-1"
    assert matches[1] == "bottom-full mb-1"


def test_standalone_environment_row_poll_keeps_bottom_full_no_500():
    from tests.conftest import make_fake_admin
    from fastapi.testclient import TestClient

    admin = make_fake_admin()
    app, _ = _client(admin)

    environment = _make_environment(admin.username)

    with patch("app.presentation.routes.environments._env_repo") as mock_er:
        mock_er.get = AsyncMock(return_value=environment)
        resp = TestClient(app).get(f"/environments/{environment.id}/row")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    matches = _MENU_RE.findall(resp.text)
    assert matches == ["bottom-full mb-1"]
