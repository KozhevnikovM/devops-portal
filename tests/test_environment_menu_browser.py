"""Optional real Chromium checks for the environment row action menu (#538).

pip install playwright && playwright install chromium. The rows are the real server-rendered
partial inside a genuinely overflowing table, so the clipping the issue reports would show up.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_environment_extend_ui import _OWNER_USER, _env

# Net listener/observer registrations made by environment_menu.js; all must return to zero
# once no menu is open, however many times rows are swapped out from under an open menu.
_INSTRUMENT = """
window.menuListeners = { resize: 0, scroll: 0, observers: 0 };
// Only count the menu script's own registrations (Playwright injects observers of its own).
const mine = () => new Error().stack.includes("environment_menu.js");
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


def _rows(count):
    from app.presentation.routes.environments import _annotate
    from app.presentation.templating import templates

    template = templates.get_template("partials/environment_row.html")
    environments = []
    for _ in range(count):
        env = _env()
        env.id = uuid4()
        environments.append(_annotate(env))
    return environments, "".join(
        template.render(environment=e, current_user=_OWNER_USER) for e in environments
    )


@pytest.fixture
def menu_page():
    playwright = pytest.importorskip("playwright.sync_api")
    environments, rows = _rows(12)
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            )
        except playwright.Error as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": 360, "height": 500})
        # The list section's real shape: an overflow-x-auto wrapper narrower than its table.
        page.set_content(
            '<!doctype html><body style="margin:0"><input id="environments-label-filter">'
            '<section id="environments-section"><div id="scroller" '
            'style="overflow-x:auto;width:330px;margin:0 15px">'
            '<table style="width:900px"><tbody id="environments-tbody">'
            + rows
            + '</tbody></table></div></section><div style="height:600px"></div></body>'
        )
        stylesheet = Path("app/static/css/tailwind.css")
        if stylesheet.exists():
            page.add_style_tag(path=str(stylesheet.resolve()))
        page.set_default_timeout(5000)
        page.add_script_tag(content=_INSTRUMENT)
        page.add_script_tag(path=str(Path("frontend/js/environment_menu.js").resolve()))
        # Scroll the table to its Actions column, as a user must before opening a menu.
        page.locator("#scroller").evaluate("(e) => e.scrollLeft = e.scrollWidth")
        page.environments = environments
        yield page
        browser.close()


def _menu(page, env):
    return page.locator(f"#environment-{env.id}-menu")


def _trigger(page, env):
    return page.locator(f"#environment-{env.id}-menu-trigger")


def _open(page, env):
    _trigger(page, env).click()
    menu = _menu(page, env)
    page.wait_for_function(
        "(m) => m.matches(':popover-open') && m.style.visibility === ''",
        arg=menu.element_handle(),
    )
    return menu


def _assert_in_viewport(page, menu):
    viewport = page.viewport_size
    box = menu.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]
    assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"]


def _assert_actions_usable(menu):
    for control in (
        menu.locator('form[hx-patch] input[name="name"]'),
        menu.locator("form[hx-put] select"),
        menu.get_by_role("button", name="Release"),
    ):
        control.scroll_into_view_if_needed()
        assert control.is_visible()
        assert control.evaluate(_HIT), "action is clipped or covered"


def test_menu_escapes_scrolling_table_near_every_edge(menu_page):
    page = menu_page
    first, last = page.environments[0], page.environments[-1]

    # First row: nothing above it inside the table, which used to clip the menu.
    menu = _open(page, first)
    _assert_in_viewport(page, menu)
    _assert_actions_usable(menu)
    trigger_box = _trigger(page, first).bounding_box()
    assert menu.bounding_box()["y"] >= trigger_box["y"] + trigger_box["height"]
    page.keyboard.press("Escape")

    # Last row scrolled to the bottom edge of the viewport opens upward, still fully visible.
    page.evaluate(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
        " window.scrollBy(0, r.bottom - window.innerHeight + 4); }",
        f"environment-{last.id}-menu-trigger",
    )
    menu = _open(page, last)
    _assert_in_viewport(page, menu)
    _assert_actions_usable(menu)
    assert (
        menu.bounding_box()["y"] + menu.bounding_box()["height"]
        <= _trigger(page, last).bounding_box()["y"]
    )

    # Scrolling the table and resizing keep the open menu anchored and inside the viewport.
    page.locator("#scroller").evaluate("(e) => e.scrollLeft -= 20")
    page.wait_for_timeout(50)
    trigger_box, box = _trigger(page, last).bounding_box(), menu.bounding_box()
    # Right-aligned to the moved trigger unless that would push it past the viewport edge.
    expected = min(trigger_box["x"] + trigger_box["width"], 360 - 8) - box["width"]
    assert box["x"] == pytest.approx(max(8, expected), abs=1)
    _assert_in_viewport(page, menu)
    page.set_viewport_size({"width": 320, "height": 420})
    page.wait_for_timeout(50)
    if menu.evaluate("(m) => m.matches(':popover-open')"):
        _assert_in_viewport(page, menu)

    # A trigger scrolled out of the visible table closes the menu instead of leaving it floating.
    page.locator("#scroller").evaluate("(e) => e.scrollLeft = 0")
    page.wait_for_function(
        "(m) => !m.matches(':popover-open')", arg=menu.element_handle()
    )
    assert page.evaluate("window.menuListeners") == _NO_LISTENERS


def test_short_viewport_scrolls_menu_internally(menu_page):
    page = menu_page
    # Too short for the menu on either side of a mid-viewport trigger.
    page.set_viewport_size({"width": 360, "height": 140})
    env = page.environments[5]
    page.evaluate(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
        " window.scrollBy(0, r.top - 60); }",
        f"environment-{env.id}-menu-trigger",
    )
    menu = _open(page, env)
    _assert_in_viewport(page, menu)
    assert menu.evaluate("(m) => m.scrollHeight > m.clientHeight"), "menu should scroll"
    # Scrolling inside the menu must not re-anchor or close it.
    page.wait_for_timeout(50)
    _assert_actions_usable(menu)
    assert menu.evaluate("(m) => m.matches(':popover-open')")


def test_keyboard_escape_and_outside_click(menu_page):
    page = menu_page
    env = page.environments[0]
    trigger, menu = _trigger(page, env), _menu(page, env)
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "(m) => m.matches(':popover-open')", arg=menu.element_handle()
    )
    page.keyboard.press("Tab")
    assert menu.evaluate("(m) => m.contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    assert trigger.evaluate("(e) => e === document.activeElement")
    page.keyboard.press("Space")
    page.wait_for_function(
        "(m) => m.matches(':popover-open')", arg=menu.element_handle()
    )
    trigger.click()  # the trigger toggles its own menu closed
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    _open(page, env)
    page.mouse.click(2, 2)
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    # Opening another row's menu closes the first; only one is ever tracked.
    _open(page, env)
    other = _open(page, page.environments[1])
    assert not menu.evaluate("(m) => m.matches(':popover-open')")
    page.keyboard.press("Escape")
    assert not other.evaluate("(m) => m.matches(':popover-open')")
    assert page.evaluate("window.menuListeners") == _NO_LISTENERS


def test_row_and_section_swaps_dispose_without_leaks(menu_page):
    page = menu_page
    env = page.environments[0]
    for attempt in range(12):
        menu = _open(page, env)
        assert page.evaluate("window.menuListeners") == _ONE_MENU_LISTENERS
        # An unrelated DOM update (another row's reconcile) leaves the open menu alone.
        page.evaluate(
            "document.getElementById('environments-label-filter').value = 'x'"
        )
        page.locator("#environments-tbody").evaluate(
            "(t) => t.appendChild(document.createElement('tr'))"
        )
        page.wait_for_timeout(20)
        assert menu.evaluate("(m) => m.matches(':popover-open')")
        # Focus inside the menu, then swap the row as an HTMX/SSE outerHTML response would.
        menu.locator('input[name="name"]').focus()
        page.evaluate(
            """([id, attempt]) => {
            const row = document.getElementById(id);
            const clone = row.cloneNode(true);
            clone.querySelector('input[name="name"]').value = 'renamed-' + attempt;
            row.replaceWith(clone);
        }""",
            [f"environment-{env.id}", attempt],
        )
        page.wait_for_function("window.menuListeners.observers === 0")
        assert page.evaluate("window.menuListeners") == _NO_LISTENERS
        assert page.locator(":popover-open").count() == 0
        assert _trigger(page, env).evaluate("(e) => e === document.activeElement")
        assert (
            _menu(page, env).locator('input[name="name"]').input_value()
            == f"renamed-{attempt}"
        )

    # Replacing the whole list section (a filter change) disposes the open menu too.
    _open(page, page.environments[3])
    page.evaluate(
        "document.getElementById('environments-section').outerHTML ="
        " '<section id=\"environments-section\"></section>'"
    )
    page.wait_for_function("window.menuListeners.observers === 0")
    assert page.evaluate("window.menuListeners") == _NO_LISTENERS
    assert page.locator(":popover-open").count() == 0
