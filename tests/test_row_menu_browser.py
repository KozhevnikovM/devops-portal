"""Real-Chromium checks for the row action menus (#538, #542): environment and booking rows.

Run with `pytest -m browser` after `npm run build:static`; needs `pip install -r
requirements-browser.txt && playwright install chromium`. The `browser-tests` CI job sets
REQUIRE_BROWSER_TESTS=1, which turns every skip here into a failure (tests/browser_plugin.py).

Each test drives the real application: Playwright routes http://localhost:8000 (BASE_URL, so the
CSRF origin check passes) to the FastAPI app with its repositories mocked, so the page HTML,
`base.html`'s `<script src>`/`<link>` tags and the `/static` mount are exactly what production
serves. That is how #542's centred-menu bug hid: the old suite injected the script directly and
never noticed when a page couldn't load it. A narrow viewport makes the bookings/environments
table genuinely overflow its `overflow-x-auto` wrapper.
"""

import re
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import EnvironmentPage, KeysetPage

pytestmark = pytest.mark.browser

ORIGIN = "http://localhost:8000"
GAP = 4  # row_menu.js: distance between trigger and menu
NARROW = {"width": 360, "height": 640}
STATIC = Path("app/static")

# Net listener/observer registrations made by row_menu.js; all must return to zero once no menu
# is open, however many times rows are swapped out from under an open menu.
_INSTRUMENT = """
window.menuListeners = { resize: 0, scroll: 0, observers: 0 };
// Only count the menu script's own registrations (Playwright and htmx register their own).
const mine = () => new Error().stack.includes("row_menu.js");
const add = EventTarget.prototype.addEventListener;
const remove = EventTarget.prototype.removeEventListener;
EventTarget.prototype.addEventListener = function (type, ...rest) {
    if (type in window.menuListeners && mine()) window.menuListeners[type]++;
    return add.call(this, type, ...rest);
};
EventTarget.prototype.removeEventListener = function (type, ...rest) {
    if (type in window.menuListeners && mine()) window.menuListeners[type]--;
    return remove.call(this, type, ...rest);
};
const observe = MutationObserver.prototype.observe;
const disconnect = MutationObserver.prototype.disconnect;
MutationObserver.prototype.observe = function (...args) {
    if (mine()) window.menuListeners.observers++;
    return observe.apply(this, args);
};
MutationObserver.prototype.disconnect = function () {
    if (mine()) window.menuListeners.observers--;
    return disconnect.call(this);
};
"""
_NO_LISTENERS = {"resize": 0, "scroll": 0, "observers": 0}
_ONE_MENU_LISTENERS = {"resize": 1, "scroll": 1, "observers": 1}

# Whether a point at the centre of the element hits the element itself (visible, unclipped).
_HIT = """(e) => { const r = e.getBoundingClientRect();
    return e.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); }"""

# Samples the menu and trigger rectangles on every animation frame from the click until it
# settles, recording only frames in which the menu is open and visible.
_RECORD_FRAMES = """([menuId, triggerId]) => {
    window.menuFrames = [];
    const menu = document.getElementById(menuId), trigger = document.getElementById(triggerId);
    let frames = 0;
    (function sample() {
        if (menu.matches(':popover-open') && getComputedStyle(menu).visibility !== 'hidden') {
            const m = menu.getBoundingClientRect(), t = trigger.getBoundingClientRect();
            window.menuFrames.push([[m.x, m.y, m.width, m.height], [t.x, t.y, t.width, t.height]]);
        }
        if (++frames < 30) requestAnimationFrame(sample);
    })();
}"""


# ── The app behind the browser ────────────────────────────────────────────────


def _booking(
    owner, status=BookingStatus.READY, resource_type=ResourceType.VM
) -> Booking:
    now = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        user_id=str(owner.id),
        status=status,
        resource_type=resource_type,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
        vm_ip="10.0.0.1",
        owner_username=owner.username,
        namespace_name="team-a" if resource_type == ResourceType.NAMESPACE else None,
        cluster_name="c1" if resource_type == ResourceType.NAMESPACE else None,
    )


class Portal:
    """The application with mocked repositories, served to the page through Playwright."""

    def __init__(self, page, user):
        from fastapi.testclient import TestClient

        from app.infrastructure.auth import require_user
        from app.infrastructure.database.session import get_async_session
        from app.main import app

        self.page, self.user, self.app = page, user, app
        self.bookings: dict = {}  # id -> Booking, what the booking repo returns
        self.listed: list = []  # what the bookings list returns, in order
        self.environments: list = []
        self.requests: list = []  # (method, path, form) of every mutating request
        self.block_menu_script = False
        app.dependency_overrides[get_async_session] = lambda: AsyncMock()
        app.dependency_overrides[require_user] = lambda: user
        self._stack = ExitStack()
        self._mock_repos()
        # Not entered as a context manager: that would run the app's startup (DB seeding).
        self.client = TestClient(app)
        page.add_init_script(_INSTRUMENT)
        page.route(f"{ORIGIN}/**", self._handle)

    def _mock_repos(self):
        def mock(target):
            return self._stack.enter_context(patch(target))

        repo = mock("app.presentation.routes.bookings._repo")
        repo.list_page = AsyncMock(
            side_effect=lambda *a, **k: KeysetPage(items=list(self.listed))
        )
        repo.get = AsyncMock(
            side_effect=lambda session, booking_id: self.bookings[booking_id]
        )
        repo.queue_positions = AsyncMock(return_value={})
        for name in ("_image_repo", "_hw_config_repo", "_role_repo"):
            mock(f"app.presentation.routes.bookings.{name}").list_active = AsyncMock(
                return_value=[]
            )
        for name in ("_namespace_repo", "_static_vm_repo"):
            mock(f"app.presentation.routes.bookings.{name}").list_available = AsyncMock(
                return_value=[]
            )
        env_repo = mock("app.presentation.routes.environments._env_repo")
        env_repo.list_page = AsyncMock(
            side_effect=lambda *a, **k: EnvironmentPage(items=list(self.environments))
        )
        env_repo.get = AsyncMock(
            side_effect=lambda session, env_id: next(
                e for e in self.environments if e.id == env_id
            )
        )
        mock(
            "app.presentation.routes.environments._blueprint_repo"
        ).list_active = AsyncMock(return_value=[])
        ns = mock("app.presentation.routes.environments._namespace_repo")
        ns.list_available = AsyncMock(return_value=[])
        ns.list_held_standalone_by_user = AsyncMock(return_value=[])

    def close(self):
        self.app.dependency_overrides.clear()
        self._stack.close()

    def add_bookings(self, *bookings):
        for booking in bookings:
            self.bookings[booking.id] = booking
            self.listed.append(booking)

    def _row(self, booking):
        return self.client.get(f"/bookings/{booking.id}/row")

    def _handle(self, route, request):
        url = urlsplit(request.url)
        path = url.path + (f"?{url.query}" if url.query else "")
        if url.path == "/events/stream" or url.path.endswith("/reconcile"):
            route.fulfill(status=204)  # no live updates or reconciliation during a test
            return
        if self.block_menu_script and url.path == "/static/js/row_menu.js":
            route.fulfill(status=404, body="not found")
            return
        # Booking mutations: the browser sends the real HTMX request; the response is the row the
        # real /row endpoint renders after applying the change, as the real handler would.
        match = re.fullmatch(r"/bookings/([0-9a-f-]+)(/extend|/label)?", url.path)
        if match and request.method in ("PUT", "PATCH", "DELETE"):
            form = {k: v[0] for k, v in parse_qs(request.post_data or "").items()}
            self.requests.append((request.method, url.path, form))
            booking = next(
                b for b in self.bookings.values() if str(b.id) == match.group(1)
            )
            if match.group(2) == "/extend":
                booking.expires_at += timedelta(minutes=int(form["extend_minutes"]))
            elif match.group(2) == "/label":
                booking.label = form["label"]
            else:
                booking.status = BookingStatus.RELEASED
            response = self._row(booking)
        else:
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() in ("accept", "content-type", "origin", "referer")
                or k.startswith("hx-")
            }
            response = self.client.request(
                request.method, path, headers=headers, content=request.post_data_buffer
            )
        route.fulfill(
            status=response.status_code,
            headers={"content-type": response.headers.get("content-type", "text/html")},
            body=response.content,
        )


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    if (
        not (STATIC / "js" / "row_menu.js").exists()
        or not (STATIC / "js" / "htmx.min.js").exists()
    ):
        pytest.skip("app/static is not built; run `npm run build:static`")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, args=["--no-sandbox"])
        except playwright.Error as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def portal_factory(browser):
    portals = []

    def make(user, viewport=NARROW):
        page = browser.new_page(viewport=viewport)
        page.set_default_timeout(15000)  # generous: CI runners can be slow
        portal = Portal(page, user)
        portals.append(portal)
        return portal

    yield make
    for portal in portals:
        portal.page.close()
        portal.close()


def _admin():
    from tests.conftest import make_fake_admin

    return make_fake_admin()


def _goto(portal, path):
    portal.page.goto(f"{ORIGIN}{path}")
    # The table is wider than the narrow viewport; scroll it to its Actions column, as a user must.
    portal.page.locator(".overflow-x-auto").first.evaluate(
        "(e) => e.scrollLeft = e.scrollWidth"
    )


# ── Menu helpers ──────────────────────────────────────────────────────────────


def _ids(kind, record):
    return f"{kind}-{record.id}-menu", f"{kind}-{record.id}-menu-trigger"


def _open(page, kind, record):
    menu_id, trigger_id = _ids(kind, record)
    page.evaluate(_RECORD_FRAMES, [menu_id, trigger_id])
    page.locator(f"#{trigger_id}").click()
    menu = page.locator(f"#{menu_id}")
    page.wait_for_function(
        "(m) => m.matches(':popover-open') && getComputedStyle(m).visibility !== 'hidden'",
        arg=menu.element_handle(),
    )
    page.wait_for_function(
        "window.menuFrames.length > 0 && requestAnimationFrame(() => 0)"
    )
    page.wait_for_timeout(100)  # let the frame recorder see the settled position too
    return menu


def _side(menu_box, trigger_box, tolerance=1.5):
    """'below' or 'above' when the menu is adjacent to its trigger, else None."""
    m_x, m_y, m_w, m_h = menu_box
    t_x, t_y, t_w, t_h = trigger_box
    if m_x >= t_x + t_w or m_x + m_w <= t_x:
        return None  # no horizontal overlap with the trigger
    if 0 <= m_y - (t_y + t_h) <= GAP + tolerance:
        return "below"
    if 0 <= t_y - (m_y + m_h) <= GAP + tolerance:
        return "above"
    return None


def _box(locator):
    box = locator.bounding_box()
    return box["x"], box["y"], box["width"], box["height"]


def _assert_in_viewport(page, menu):
    viewport = page.viewport_size
    x, y, w, h = _box(menu)
    assert x >= -0.5 and x + w <= viewport["width"] + 0.5, (x, w, viewport)
    assert y >= -0.5 and y + h <= viewport["height"] + 0.5, (y, h, viewport)


def _assert_anchored(page, kind, record, expect=None):
    """The open menu is beside its trigger, fully on screen, and was never anywhere else."""
    menu_id, trigger_id = _ids(kind, record)
    menu, trigger = page.locator(f"#{menu_id}"), page.locator(f"#{trigger_id}")
    side = _side(_box(menu), _box(trigger))
    assert side, f"menu {_box(menu)} is not beside its trigger {_box(trigger)}"
    if expect:
        assert side == expect
    _assert_in_viewport(page, menu)
    frames = page.evaluate("window.menuFrames")
    assert frames, "the menu was never visible"
    for menu_box, trigger_box in frames:
        assert _side(menu_box, trigger_box), f"a visible frame was detached: {menu_box}"
    # The controller, not the CSS fallback, placed it: the inline override is in effect.
    styles = menu.evaluate(
        "(m) => [getComputedStyle(m).positionArea, getComputedStyle(m).positionTryFallbacks]"
    )
    assert styles == ["none", "none"]
    return menu


def _assert_reanchored(page, kind, record):
    page.wait_for_timeout(60)
    menu_id, trigger_id = _ids(kind, record)
    menu, trigger = page.locator(f"#{menu_id}"), page.locator(f"#{trigger_id}")
    assert _side(_box(menu), _box(trigger)), (_box(menu), _box(trigger))
    _assert_in_viewport(page, menu)


def _assert_usable(menu, *controls):
    for control in controls:
        locator = menu.locator(control) if isinstance(control, str) else control
        locator.scroll_into_view_if_needed()
        assert locator.is_visible()
        assert locator.evaluate(_HIT), f"{control} is clipped or covered"


def _scroll_row_to_bottom(page, trigger_id):
    page.evaluate(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
        " window.scrollBy(0, r.bottom - window.innerHeight + 4); }",
        trigger_id,
    )


def _scroll_row_to_top(page, trigger_id):
    # The order form sits above the table; bring the row up so there is room below it.
    page.evaluate(
        "(id) => window.scrollBy(0, document.getElementById(id).getBoundingClientRect().top - 80)",
        trigger_id,
    )


def _scroll_row_to_middle(page, trigger_id):
    # Room above and below, so scrolling the page or table a little keeps the trigger in view.
    page.evaluate(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
        " window.scrollBy(0, r.top - window.innerHeight / 2); }",
        trigger_id,
    )


def _no_listeners(page):
    # The popover `toggle` event that triggers disposal fires asynchronously after closing.
    page.wait_for_function(
        "Object.values(window.menuListeners).every((count) => count === 0)"
    )
    assert page.evaluate("window.menuListeners") == _NO_LISTENERS


def _closed_and_disposed(page):
    _no_listeners(page)
    assert page.locator(":popover-open").count() == 0


# ── Environments (#538) ───────────────────────────────────────────────────────


@pytest.fixture
def environments(portal_factory):
    from app.presentation.routes.environments import _annotate
    from tests.test_environment_extend_ui import _OWNER_USER, _env

    portal = portal_factory(_OWNER_USER)
    for _ in range(12):
        env = _env()
        env.id = uuid4()
        portal.environments.append(_annotate(env))
    _goto(portal, "/environments")
    return portal


def test_environment_menu_anchored_at_first_and_last_rows(environments):
    page = environments.page
    first, last = environments.environments[0], environments.environments[-1]

    _scroll_row_to_top(page, _ids("environment", first)[1])
    menu = _open(page, "environment", first)
    _assert_anchored(page, "environment", first, expect="below")
    _assert_usable(menu, 'form[hx-patch] input[name="name"]', "form[hx-put] select")
    _assert_usable(menu, menu.get_by_role("button", name="Release"))
    page.keyboard.press("Escape")

    _scroll_row_to_bottom(page, _ids("environment", last)[1])
    menu = _open(page, "environment", last)
    _assert_anchored(page, "environment", last, expect="above")
    _assert_usable(menu, menu.get_by_role("button", name="Release"))
    page.keyboard.press("Escape")

    # Page scroll, table scroll and resize keep the open menu beside its trigger.
    mid = environments.environments[len(environments.environments) // 2]
    _scroll_row_to_middle(page, _ids("environment", mid)[1])
    menu = _open(page, "environment", mid)
    _assert_anchored(page, "environment", mid)
    for dy in (40, -80):
        page.evaluate(f"window.scrollBy(0, {dy})")
        _assert_reanchored(page, "environment", mid)
    page.locator(".overflow-x-auto").first.evaluate("(e) => e.scrollLeft -= 20")
    _assert_reanchored(page, "environment", mid)
    page.set_viewport_size({"width": 340, "height": 600})
    if menu.evaluate("(m) => m.matches(':popover-open')"):
        _assert_reanchored(page, "environment", mid)

    # A trigger scrolled out of the visible table closes the menu instead of leaving it floating.
    page.locator(".overflow-x-auto").first.evaluate("(e) => e.scrollLeft = 0")
    page.wait_for_function(
        "(m) => !m.matches(':popover-open')", arg=menu.element_handle()
    )
    _no_listeners(page)


def test_environment_menu_anchored_on_desktop(portal_factory):
    from app.presentation.routes.environments import _annotate
    from tests.test_environment_extend_ui import _OWNER_USER, _env

    portal = portal_factory(_OWNER_USER, viewport={"width": 1280, "height": 720})
    env = _env()
    portal.environments.append(_annotate(env))
    portal.page.goto(f"{ORIGIN}/environments")
    _open(portal.page, "environment", env)
    _assert_anchored(portal.page, "environment", env, expect="below")


def test_environment_menu_keyboard_escape_and_outside_click(environments):
    page = environments.page
    env, other_env = environments.environments[0], environments.environments[1]
    menu_id, trigger_id = _ids("environment", env)
    trigger, menu = page.locator(f"#{trigger_id}"), page.locator(f"#{menu_id}")
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "(m) => m.matches(':popover-open')", arg=menu.element_handle()
    )
    # Escape from a control inside the menu closes it and returns focus to the trigger.
    menu.locator('input[name="name"]').focus()
    assert menu.evaluate("(m) => m.contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    assert trigger.evaluate("(e) => e === document.activeElement")
    _open(page, "environment", env)
    page.mouse.click(2, 2)
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    # Opening another row's menu closes the first; only one is ever tracked.
    _open(page, "environment", env)
    other = _open(page, "environment", other_env)
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    page.keyboard.press("Escape")
    assert not other.evaluate("(m) => m.matches(':popover-open')")
    _no_listeners(page)


def test_environment_row_and_section_swaps_dispose_without_leaks(environments):
    page = environments.page
    env = environments.environments[0]
    for attempt in range(6):
        _open(page, "environment", env)
        assert page.evaluate("window.menuListeners") == _ONE_MENU_LISTENERS
        page.locator(f"#environment-{env.id}-menu input[name='name']").focus()
        # The row's live-update swap: the real /row response, swapped in by htmx.
        page.evaluate(
            "(id) => htmx.ajax('GET', `/environments/${id}/row`,"
            " {target: `#environment-${id}`, swap: 'outerHTML'})",
            str(env.id),
        )
        _closed_and_disposed(page)
        trigger_id = _ids("environment", env)[1]
        page.wait_for_function(f"document.activeElement?.id === '{trigger_id}'")

    # Replacing the whole list section (a filter change) disposes the open menu too.
    _open(page, "environment", environments.environments[3])
    page.evaluate(
        "document.getElementById('environments-section').outerHTML ="
        " '<section id=\"environments-section\"></section>'"
    )
    _closed_and_disposed(page)


# ── Bookings: /book/vm and /book/namespace (#542) ─────────────────────────────


@pytest.fixture
def bookings(portal_factory):
    """An admin's VM page: READY rows plus one of every other menu variant."""
    admin = _admin()
    someone = _admin()
    someone.username = "someone"
    portal = portal_factory(admin)
    ready = [_booking(admin) for _ in range(12)]
    portal.failed = _booking(admin, BookingStatus.FAILED)
    portal.queued = _booking(admin, BookingStatus.QUEUED)
    portal.provisioning = _booking(someone, BookingStatus.PROVISIONING)
    portal.add_bookings(
        ready[0], portal.failed, portal.queued, portal.provisioning, *ready[1:]
    )
    portal.ready = ready
    _goto(portal, "/book/vm")
    return portal


def test_booking_menu_anchored_at_first_and_last_rows(bookings):
    page = bookings.page
    first, last = bookings.listed[0], bookings.listed[-1]

    _scroll_row_to_top(page, _ids("booking", first)[1])
    menu = _open(page, "booking", first)
    _assert_anchored(page, "booking", first, expect="below")
    _assert_usable(menu, 'input[name="label"]', 'select[name="extend_minutes"]')
    _assert_usable(menu, menu.get_by_role("button", name="Release"))
    page.keyboard.press("Escape")

    _scroll_row_to_bottom(page, _ids("booking", last)[1])
    menu = _open(page, "booking", last)
    _assert_anchored(page, "booking", last, expect="above")
    _assert_usable(menu, menu.get_by_role("button", name="Release"))
    page.keyboard.press("Escape")

    # Page scroll, table scroll and resize keep the open menu beside its trigger.
    mid = bookings.listed[len(bookings.listed) // 2]
    _scroll_row_to_middle(page, _ids("booking", mid)[1])
    menu = _open(page, "booking", mid)
    _assert_anchored(page, "booking", mid)
    for dy in (40, -80):
        page.evaluate(f"window.scrollBy(0, {dy})")
        _assert_reanchored(page, "booking", mid)
    page.locator(".overflow-x-auto").first.evaluate("(e) => e.scrollLeft -= 20")
    _assert_reanchored(page, "booking", mid)
    page.set_viewport_size({"width": 340, "height": 600})
    if menu.evaluate("(m) => m.matches(':popover-open')"):
        _assert_reanchored(page, "booking", mid)
    page.keyboard.press("Escape")
    _no_listeners(page)


def test_booking_menu_anchored_on_load_more_row(bookings):
    page = bookings.page
    from app.domain.pagination import KeysetCursor
    from app.presentation.pagination import encode_cursor

    appended = _booking(bookings.user)
    bookings.listed[:] = [appended]
    bookings.bookings[appended.id] = appended
    cursor = encode_cursor(KeysetCursor(created_at=datetime.now(UTC), id=uuid4()))
    page.evaluate(
        "(cursor) => htmx.ajax('GET', `/book/vm/rows?cursor=${cursor}`,"
        " {target: '#bookings-list', swap: 'beforeend'})",
        cursor,
    )
    page.wait_for_selector(f"#booking-{appended.id}")
    _scroll_row_to_bottom(page, _ids("booking", appended)[1])
    page.locator(".overflow-x-auto").first.evaluate(
        "(e) => e.scrollLeft = e.scrollWidth"
    )
    _open(page, "booking", appended)
    _assert_anchored(page, "booking", appended)


def test_namespace_page_menu_anchored(portal_factory):
    admin = _admin()
    portal = portal_factory(admin)
    rows = [_booking(admin, resource_type=ResourceType.NAMESPACE) for _ in range(3)]
    portal.add_bookings(*rows)
    _goto(portal, "/book/namespace")
    _scroll_row_to_top(portal.page, _ids("booking", rows[0])[1])
    menu = _open(portal.page, "booking", rows[0])
    _assert_anchored(portal.page, "booking", rows[0], expect="below")
    _assert_usable(menu, menu.get_by_role("button", name="Release"))


def test_booking_menu_scrolls_internally_in_a_short_viewport(bookings):
    page = bookings.page
    page.set_viewport_size({"width": 360, "height": 150})
    row = bookings.ready[5]
    page.evaluate(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
        " window.scrollBy(0, r.top - 60); }",
        _ids("booking", row)[1],
    )
    menu = _open(page, "booking", row)
    _assert_anchored(page, "booking", row)
    assert menu.evaluate("(m) => m.scrollHeight > m.clientHeight"), "menu should scroll"
    page.wait_for_timeout(50)
    _assert_usable(menu, menu.get_by_role("button", name="Release"))
    assert menu.evaluate("(m) => m.matches(':popover-open')")


def test_every_booking_menu_variant_escapes_the_table(bookings):
    page = bookings.page
    variants = [
        (bookings.ready[0], "Release"),
        (bookings.failed, "Force release"),
        (bookings.queued, "Cancel"),
        (bookings.provisioning, "Delete"),
    ]
    for booking, action in variants:
        menu = _open(page, "booking", booking)
        _assert_anchored(page, "booking", booking)
        _assert_usable(menu, menu.get_by_role("button", name=action))
        page.keyboard.press("Escape")
        assert not menu.evaluate("(m) => m.matches(':popover-open')")
    failed_menu = _open(page, "booking", bookings.failed)
    _assert_usable(failed_menu, failed_menu.get_by_role("link", name="Audit log"))
    page.mouse.click(2, 2)  # outside click closes it
    assert not failed_menu.evaluate("(m) => m.matches(':popover-open')")
    _no_listeners(page)


def test_label_save_from_the_popover_replaces_the_row(bookings):
    page = bookings.page
    booking = bookings.ready[0]
    menu = _open(page, "booking", booking)
    menu.locator('input[name="label"]').fill("renamed-box")
    menu.get_by_role("button", name="Save").first.click()
    page.wait_for_function(
        f"document.getElementById('booking-{booking.id}')?.textContent.includes('renamed-box')"
    )
    assert bookings.requests[-1] == (
        "PATCH",
        f"/bookings/{booking.id}/label",
        {"label": "renamed-box"},
    )
    _closed_and_disposed(page)
    _open(page, "booking", booking)
    _assert_anchored(page, "booking", booking)


def test_extend_from_the_popover_sends_htmx_request_and_replaces_the_row(bookings):
    page = bookings.page
    booking = bookings.ready[0]
    from app.presentation.templating import templates

    as_tz = templates.env.filters["as_tz"]
    timezone = bookings.user.timezone
    expected = str(as_tz(booking.expires_at + timedelta(minutes=1440), timezone))
    assert expected not in page.locator(f"#booking-{booking.id}").inner_text()
    menu = _open(page, "booking", booking)
    menu.locator('select[name="extend_minutes"]').select_option("1440")
    menu.get_by_role("button", name="Extend").click()
    # Wait for the swapped-in row: opening the menu already changes the row's text.
    page.wait_for_function(
        "([id, text]) => document.getElementById(id)?.innerText.includes(text)",
        arg=[f"booking-{booking.id}", expected],
    )
    assert bookings.requests[-1] == (
        "PUT",
        f"/bookings/{booking.id}/extend",
        {"extend_minutes": "1440"},
    )
    assert str(as_tz(booking.expires_at, timezone)) == expected
    _closed_and_disposed(page)
    _open(page, "booking", booking)
    _assert_anchored(page, "booking", booking)


def test_live_replacement_with_actions_disposes_and_reopens(bookings):
    page = bookings.page
    booking = bookings.ready[1]
    for _ in range(4):
        _open(page, "booking", booking)
        assert page.evaluate("window.menuListeners") == _ONE_MENU_LISTENERS
        page.evaluate(
            "(id) => htmx.ajax('GET', `/bookings/${id}/row`,"
            " {target: `#booking-${id}`, swap: 'outerHTML'})",
            str(booking.id),
        )
        _closed_and_disposed(page)
    _open(page, "booking", booking)
    _assert_anchored(page, "booking", booking)


def test_filter_change_removing_the_booking_leaves_nothing_behind(bookings):
    page = bookings.page
    booking = bookings.ready[2]
    _open(page, "booking", booking)
    bookings.listed[:] = [b for b in bookings.listed if b is not booking]
    page.evaluate(
        "htmx.ajax('GET', '/book/vm/list?filter=all', {target: '#bookings-section', swap: 'outerHTML'})"
    )
    page.wait_for_selector(f"#booking-{booking.id}", state="detached")
    _closed_and_disposed(page)
    assert page.locator(f"#{_ids('booking', booking)[1]}").count() == 0


def test_release_with_confirm_leaves_a_row_without_a_menu(bookings):
    page = bookings.page
    booking = bookings.ready[3]
    confirms = []
    page.on("dialog", lambda dialog: (confirms.append(dialog.message), dialog.accept()))
    menu = _open(page, "booking", booking)
    menu.get_by_role("button", name="Release").click()
    page.wait_for_function(
        f"document.getElementById('booking-{booking.id}')?.textContent.includes('RELEASED')"
    )
    assert confirms == ["Release this VM? The machine will be destroyed."]
    assert bookings.requests[-1] == ("DELETE", f"/bookings/{booking.id}", {})
    _closed_and_disposed(page)
    assert page.locator(f"#{_ids('booking', booking)[1]}").count() == 0


# ── Without the placement script (CSS anchor fallback) ────────────────────────


def _assert_css_anchored(page, kind, record):
    menu_id, trigger_id = _ids(kind, record)
    menu, trigger = page.locator(f"#{menu_id}"), page.locator(f"#{trigger_id}")
    page.locator(f"#{trigger_id}").click()
    page.wait_for_function(
        "(m) => m.matches(':popover-open')", arg=menu.element_handle()
    )
    assert _side(_box(menu), _box(trigger)), (
        f"menu {_box(menu)} not beside {_box(trigger)}"
    )
    # Placed by the stylesheet's anchor rule, not by the (unloaded) controller.
    assert menu.evaluate("(m) => getComputedStyle(m).positionArea") != "none"
    _assert_in_viewport(page, menu)
    viewport = page.viewport_size
    x, y, w, h = _box(menu)
    centred = (
        abs(x + w / 2 - viewport["width"] / 2) < 2
        and abs(y + h / 2 - viewport["height"] / 2) < 2
    )
    assert not centred
    page.keyboard.press("Escape")


def test_menus_stay_anchored_when_the_script_is_not_served(portal_factory):
    from app.presentation.routes.environments import _annotate
    from tests.test_environment_extend_ui import _OWNER_USER, _env

    portal = portal_factory(_OWNER_USER)
    portal.block_menu_script = True
    env = _env()
    portal.environments.append(_annotate(env))
    _goto(portal, "/environments")
    assert portal.page.evaluate("typeof window.menuListeners") == "object"
    _assert_css_anchored(portal.page, "environment", env)

    admin = _admin()
    portal = portal_factory(admin)
    portal.block_menu_script = True
    rows = [_booking(admin) for _ in range(12)]
    portal.add_bookings(*rows)
    _goto(portal, "/book/vm")
    _assert_css_anchored(portal.page, "booking", rows[0])
    _scroll_row_to_bottom(portal.page, _ids("booking", rows[-1])[1])
    _assert_css_anchored(
        portal.page, "booking", rows[-1]
    )  # flips above near the bottom
