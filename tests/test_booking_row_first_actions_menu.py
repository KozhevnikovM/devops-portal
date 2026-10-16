"""Row action menu markup: every ⋮ menu is a top-layer popover, whatever the row's position.

History: booking rows once picked an in-cell `top-full`/`bottom-full` menu from an
`is_first_row` flag the list set on its first row (docs/bugfix/booking-row-first-actions-menu-
hidden.md). Both sides could still be clipped by the table's overflow-x-auto wrapper, so the
environment menu became a native popover (#538) and booking menus followed (#542). Both now use
the shared `partials/row_menu.html` macro: the trigger and popover carry `data-row-menu*`
attributes for row_menu.js and CSS anchor names for base.html's no-script fallback, and the
markup is identical in a list render, a Load more page and a standalone row refresh. Placement
itself is runtime behavior, covered by tests/test_row_menu_browser.py.
"""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.domain.entities import Booking, Environment
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import EnvironmentPage, KeysetCursor, KeysetPage
from app.presentation.pagination import encode_cursor

# The old in-cell menus: absolutely positioned inside the clipping table cell.
_IN_CELL_MENU_RE = re.compile(
    r'class="absolute right-0 (top-full mt-1|bottom-full mb-1) w-48'
)


def _make_booking(
    user_id: str, owner_username: str, status=BookingStatus.READY
) -> Booking:
    now = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=status,
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
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    return app, session_mock


def _menu(html: str, menu_id: str) -> str:
    """The trigger + popover markup of one row menu."""
    match = re.search(
        rf'<button\s+type="button"\s+id="{menu_id}-trigger".*?</button>\s*'
        rf'<div\s+id="{menu_id}"\s+popover\b.*?(?=\n\s*</td>)',
        html,
        re.DOTALL,
    )
    assert match, f"no popover menu {menu_id}"
    return match.group(0)


def _assert_popover_menu(html: str, menu_id: str) -> str:
    menu = _menu(html, menu_id)
    assert f'popovertarget="{menu_id}"' in menu
    assert "data-row-menu-trigger" in menu and "data-row-menu\n" in menu
    # Per-row CSS anchor: base.html's fallback keeps the menu beside its trigger without JS.
    assert f'style="anchor-name: --{menu_id}"' in menu
    assert f'style="position-anchor: --{menu_id}"' in menu
    return menu


def _list_page(app, path, bookings):
    from fastapi.testclient import TestClient

    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings._image_repo") as mock_img,
        patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.bookings._namespace_repo") as mock_ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm,
        patch("app.presentation.routes.bookings._role_repo") as mock_role,
    ):
        mock_repo.list_page = AsyncMock(return_value=KeysetPage(items=bookings))
        mock_repo.queue_positions = AsyncMock(return_value={})
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])
        return TestClient(app).get(path)


def _row(app, booking):
    from fastapi.testclient import TestClient

    with patch("app.presentation.routes.bookings._repo") as mock_repo:
        mock_repo.get = AsyncMock(return_value=booking)
        mock_repo.queue_positions = AsyncMock(return_value={})
        return TestClient(app).get(f"/bookings/{booking.id}/row")


def test_every_booking_menu_variant_is_a_popover_with_unchanged_actions():
    """READY, FAILED, QUEUED (owner) and admin PROVISIONING menus, each with its own actions."""
    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    app, _ = _client(admin)
    ready = _make_booking(str(admin.id), admin.username)
    failed = _make_booking(str(admin.id), admin.username, BookingStatus.FAILED)
    queued = _make_booking(str(admin.id), admin.username, BookingStatus.QUEUED)
    provisioning = _make_booking(str(uuid4()), "someone", BookingStatus.PROVISIONING)
    resp = _list_page(app, "/book/vm", [ready, failed, queued, provisioning])
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert _IN_CELL_MENU_RE.findall(resp.text) == []
    assert '<details class="relative">' not in resp.text.split("<main")[-1]
    assert '<script src="/static/js/row_menu.js"></script>' in resp.text
    assert "[data-row-menu] { margin: 0; inset: auto; position-area:" in resp.text

    menu = _assert_popover_menu(resp.text, f"booking-{ready.id}-menu")
    assert f'hx-patch="/bookings/{ready.id}/label"' in menu
    assert f'hx-put="/bookings/{ready.id}/extend"' in menu
    assert (
        f'hx-delete="/bookings/{ready.id}"' in menu
        and ">\n                            Release" in menu
    )

    menu = _assert_popover_menu(resp.text, f"booking-{failed.id}-menu")
    assert f'href="/bookings/{failed.id}/audit"' in menu
    assert f'hx-post="/admin/bookings/{failed.id}/force-release"' in menu
    assert "extend" not in menu

    menu = _assert_popover_menu(resp.text, f"booking-{queued.id}-menu")
    assert f'hx-patch="/bookings/{queued.id}/label"' in menu
    assert f'hx-delete="/bookings/{queued.id}"' in menu

    menu = _assert_popover_menu(resp.text, f"booking-{provisioning.id}-menu")
    assert f'hx-delete="/bookings/{provisioning.id}"' in menu and "Force-delete" in menu
    assert "/label" not in menu


def test_menu_permission_gating_is_unchanged():
    """No menu on another user's READY row or an in-flight row for a non-admin."""
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app, _ = _client(user)
    others = _make_booking(str(uuid4()), "someone-else")
    in_flight = _make_booking(str(user.id), user.username, BookingStatus.PROVISIONING)
    own = _make_booking(str(user.id), user.username)
    resp = _list_page(app, "/book/vm", [others, in_flight, own])
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert f"booking-{others.id}-menu" not in resp.text
    assert f"booking-{in_flight.id}-menu" not in resp.text
    menu = _assert_popover_menu(resp.text, f"booking-{own.id}-menu")
    assert "force-release" not in menu


def test_list_first_row_and_standalone_refresh_have_identical_menu_markup():
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app, _ = _client(user)
    first = _make_booking(str(user.id), user.username)
    second = _make_booking(str(user.id), user.username)
    listed = _list_page(app, "/book/vm", [first, second])
    refreshed = _row(app, first)
    app.dependency_overrides.clear()

    assert listed.status_code == refreshed.status_code == 200
    menu_id = f"booking-{first.id}-menu"
    assert _menu(listed.text, menu_id) == _menu(refreshed.text, menu_id)
    # And the first row's menu is no different from any other row's.
    second_menu = _menu(listed.text, f"booking-{second.id}-menu")
    assert _menu(listed.text, menu_id).replace(str(first.id), "X").replace(
        str(first.id)[:8], "X"
    ) == second_menu.replace(str(second.id), "X").replace(str(second.id)[:8], "X")


def test_load_more_page_rows_render_popover_menus():
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app, _ = _client(user)
    bookings = [_make_booking(str(user.id), user.username) for _ in range(2)]
    cursor = encode_cursor(KeysetCursor(created_at=datetime.now(UTC), id=uuid4()))
    resp = _list_page(app, f"/book/vm/rows?cursor={cursor}", bookings)
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert _IN_CELL_MENU_RE.findall(resp.text) == []
    for booking in bookings:
        _assert_popover_menu(resp.text, f"booking-{booking.id}-menu")


def test_namespace_page_rows_render_popover_menus():
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app, _ = _client(user)
    booking = _make_booking(str(user.id), user.username)
    booking.resource_type = ResourceType.NAMESPACE
    booking.namespace_name, booking.cluster_name = "team-a", "c1"
    resp = _list_page(app, "/book/namespace", [booking])
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    menu = _assert_popover_menu(resp.text, f"booking-{booking.id}-menu")
    assert "Release this namespace? It returns to the pool." in menu


def test_standalone_row_poll_renders_popover_no_500():
    """GET /bookings/{id}/row has no list context; it must not 500 and renders the same popover."""
    from tests.conftest import make_fake_user

    user = make_fake_user()
    app, _ = _client(user)
    booking = _make_booking(str(user.id), user.username)
    resp = _row(app, booking)
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert _IN_CELL_MENU_RE.findall(resp.text) == []
    _assert_popover_menu(resp.text, f"booking-{booking.id}-menu")


# ── Environments page (#538) — same shared macro since #542 ──────────────────


def _make_environment(owner_username: str) -> Environment:
    now = datetime.now(UTC)
    return Environment(
        id=uuid4(),
        name="dev-stack",
        blueprint_name="dev-stack",
        user_id=uuid4(),
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        bookings=[],
        owner_username=owner_username,
    )


def _assert_environment_menus(html: str, environments) -> None:
    assert _IN_CELL_MENU_RE.findall(html) == []
    for environment in environments:
        menu = _assert_popover_menu(html, f"environment-{environment.id}-menu")
        assert f'hx-delete="/environments/{environment.id}"' in menu


def test_environment_list_rows_render_popover_menus():
    from fastapi.testclient import TestClient

    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    app, _ = _client(admin)

    e1 = _make_environment(admin.username)
    e2 = _make_environment(admin.username)

    with (
        patch("app.presentation.routes.environments._env_repo") as mock_er,
        patch("app.presentation.routes.environments._blueprint_repo") as mock_br,
        patch("app.presentation.routes.environments._namespace_repo") as mock_ns,
    ):
        mock_er.list_page = AsyncMock(return_value=EnvironmentPage(items=[e1, e2]))
        mock_br.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_ns.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = TestClient(app).get("/environments")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    _assert_environment_menus(resp.text, [e1, e2])
    assert '<script src="/static/js/row_menu.js"></script>' in resp.text


def test_standalone_environment_row_renders_popover_menu_no_500():
    from fastapi.testclient import TestClient

    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    app, _ = _client(admin)

    environment = _make_environment(admin.username)

    with patch("app.presentation.routes.environments._env_repo") as mock_er:
        mock_er.get = AsyncMock(return_value=environment)
        resp = TestClient(app).get(f"/environments/{environment.id}/row")

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    _assert_environment_menus(resp.text, [environment])
