"""Real-Chromium checks that a live (SSE) row update replaces its row whole (#541).

Runs with the `browser` suite (see tests/test_row_menu_browser.py for setup). Live updates go
through the real htmx-sse extension: the harness's EventSource stand-in delivers the row HTML the
app's real /row endpoint renders, framed as routes/events.py frames it. Before the fix, htmx-sse's
default innerHTML swap put the new <tr> inside the old one: a nested row and a duplicate id.

htmx-sse removes a replaced row's listener lazily, on the next event of that name, so a stale
listener can briefly sit beside the live one (design.md, Decision 1). It must never apply an
update: every event below inserts its row exactly once. Live rows swap with settle:0ms, so the new
row's own listener is registered before the next event, even one in the same tick.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.enums import BookingStatus

# The fixtures are imported so pytest registers them here; tests request them by name only.
from tests.test_row_menu_browser import (  # noqa: F401
    _ONE_MENU_LISTENERS,
    _assert_anchored,
    _booking,
    _closed_and_disposed,
    _ids,
    _open,
    bookings,
    browser,
    environments,
    portal_factory,
)

pytestmark = pytest.mark.browser

_ROW_STATE = """(id) => {
    const rows = document.querySelectorAll(`[id="${id}"]`);
    return {
        count: rows.length,
        parent: rows[0]?.parentElement?.tagName ?? null,
        nested: document.querySelectorAll("tr tr").length,
    };
}"""
_SINGLE = {"count": 1, "parent": "TBODY", "nested": 0}


def _assert_single(page, row_id):
    assert page.evaluate(_ROW_STATE, row_id) == _SINGLE


def _settled(page, row_id):
    """Wait until htmx has processed a row swapped in by anything but a live update.

    Row actions, reconciliation and list loads swap with htmx's default 20ms settle, and htmx
    registers the new row's live listener only then; an update inside that window is dropped
    (and recovered by reconciliation). Live updates themselves settle at once (settle:0ms).
    """
    page.wait_for_function(
        "(id) => { const row = document.getElementById(id);"
        " return row && !row.classList.contains('htmx-added')"
        " && !row.classList.contains('htmx-settling'); }",
        arg=row_id,
    )


def _rename(kind, record, text):
    """Change what the row displays, so each pushed update is distinguishable."""
    if kind == "booking":
        record.label = text
    else:
        record.name = text


def _push(portal, kind, record, text):
    """Push one live update showing `text`; it must replace the single row exactly once."""
    page, row_id = portal.page, f"{kind}-{record.id}"
    _rename(kind, record, text)
    page.evaluate("(id) => { window.sse.inserted[id] = 0; }", row_id)
    portal.push(kind, record)  # dispatch, and so the swap, is synchronous
    _assert_single(page, row_id)
    assert text in page.locator(f"#{row_id}").inner_text()
    assert page.evaluate("(id) => window.sse.inserted[id]", row_id) == 1
    # The live row's handler, plus at most the replaced row's not-yet-removed (inert) one.
    assert page.evaluate("(name) => window.sse.count(name)", row_id) <= 2


@pytest.fixture(params=["booking", "environment"])
def live_list(request):
    """(portal, kind, record): a displayed, live row of either list."""
    if request.param == "booking":
        portal = request.getfixturevalue("bookings")
        return portal, "booking", portal.ready[1]
    portal = request.getfixturevalue("environments")
    return portal, "environment", portal.environments[1]


# ── 1.3 / 3.1: replaced, never nested — however many updates arrive ──────────


def test_live_update_replaces_the_row(live_list):
    portal, kind, record = live_list
    row_id = f"{kind}-{record.id}"
    assert portal.page.evaluate("(name) => window.sse.count(name)", row_id) == 1
    _push(portal, kind, record, "first-update")


def test_repeated_live_updates_each_apply_once(live_list):
    portal, kind, record = live_list
    for step in range(6):
        _push(portal, kind, record, f"update-{step}")


def test_back_to_back_updates_are_both_applied(live_list):
    """A second update right behind the first (one tick, no settle in between) is not dropped."""
    portal, kind, record = live_list
    page, row_id = portal.page, f"{kind}-{record.id}"
    frames = []
    for text in ("burst-1", "burst-2"):
        _rename(kind, record, text)
        html = portal.client.get(f"/{kind}s/{record.id}/row").text
        frames.append("\n".join(html.splitlines()))
    page.evaluate("(id) => { window.sse.inserted[id] = 0; }", row_id)
    page.evaluate(
        "([name, frames]) => frames.forEach((data) => window.sse.emit(name, data))",
        [row_id, frames],
    )
    assert page.evaluate("(id) => window.sse.inserted[id]", row_id) == 2
    assert "burst-2" in page.locator(f"#{row_id}").inner_text()
    _assert_single(page, row_id)


def test_booking_lifecycle_updates_replace_the_row(request):
    portal = request.getfixturevalue("bookings")
    booking = portal.provisioning
    for step, status in enumerate(
        (BookingStatus.CONFIGURING, BookingStatus.READY, BookingStatus.RELEASING)
    ):
        booking.status = status
        _push(portal, "booking", booking, f"step-{step}")
        assert (
            status.value in portal.page.locator(f"#booking-{booking.id}").inner_text()
        )


# ── 3.2: an update under an open menu ────────────────────────────────────────


def test_live_update_under_an_open_menu_disposes_it_and_restores_focus(live_list):
    portal, kind, record = live_list
    page = portal.page
    menu_id, trigger_id = _ids(kind, record)
    for step in range(3):
        _open(page, kind, record)
        assert page.evaluate("window.menuListeners") == _ONE_MENU_LISTENERS
        page.locator(
            f"#{menu_id} input, #{menu_id} select, #{menu_id} button"
        ).first.focus()
        _push(portal, kind, record, f"under-menu-{step}")
        _closed_and_disposed(page)
        page.wait_for_function(f"document.activeElement?.id === '{trigger_id}'")
        assert page.locator(f"#{menu_id}").count() == 1
    _open(page, kind, record)
    _assert_anchored(page, kind, record)


# ── 3.3: row actions after an update ─────────────────────────────────────────


def test_row_actions_after_live_updates(live_list):
    portal, kind, record = live_list
    page, row_id = portal.page, f"{kind}-{record.id}"
    path = f"/{kind}s/{record.id}"
    _push(portal, kind, record, "before-extend")

    menu = _open(page, kind, record)
    page.evaluate("(id) => { window.sse.inserted[id] = 0; }", row_id)
    menu.locator('select[name="extend_minutes"]').select_option("1440")
    menu.get_by_role("button", name="Extend").click()
    page.wait_for_function("(id) => window.sse.inserted[id] === 1", arg=row_id)
    _settled(page, row_id)
    assert portal.requests[-1] == ("PUT", f"{path}/extend", {"extend_minutes": "1440"})
    _closed_and_disposed(page)
    _assert_single(page, row_id)

    _push(portal, kind, record, "before-release")
    page.on("dialog", lambda dialog: dialog.accept())
    menu = _open(page, kind, record)
    menu.get_by_role("button", name="Release").click()
    page.wait_for_function(
        "(id) => /RELEAS/.test(document.getElementById(id)?.textContent ?? '')",
        arg=row_id,
    )
    assert portal.requests[-1] == ("DELETE", path, {})
    _settled(page, row_id)
    _closed_and_disposed(page)
    _assert_single(page, row_id)


# ── 3.4: reconciliation after updates ────────────────────────────────────────


def _reconcile(portal, kind, *, changed=(), removed=()):
    """Apply a page-reconciliation response rendered from the real partial."""
    from app.presentation.routes import bookings as booking_routes
    from app.presentation.routes import environments as environment_routes
    from app.presentation.templating import templates

    if kind == "booking":
        context = booking_routes.new_rows_context(
            page_path="/book/vm", filter="mine", show_released=False, label=None
        )
        context["changed"] = list(changed)
        target = "#bookings-list"
    else:
        context = environment_routes.new_rows_context(
            filter="mine", show_released=False, label=None
        )
        context.update(changed=list(changed), over_limit=[])
        target = "#environments-tbody"
    context.update(current_user=portal.user, removed=[str(i) for i in removed])
    html = templates.get_template(f"partials/{kind}_reconcile.html").render(**context)
    portal.canned["/test/reconcile"] = html
    portal.page.evaluate(
        "(target) => htmx.ajax('GET', '/test/reconcile', {target, swap: 'none'})",
        target,
    )


def test_reconciliation_after_live_updates(live_list):
    portal, kind, record = live_list
    page, row_id = portal.page, f"{kind}-{record.id}"
    for step in range(2):
        _push(portal, kind, record, f"pre-reconcile-{step}")

    _rename(kind, record, "reconciled")
    _reconcile(portal, kind, changed=[record])
    page.wait_for_function(
        "(id) => document.getElementById(id)?.textContent.includes('reconciled')",
        arg=row_id,
    )
    _settled(page, row_id)
    _assert_single(page, row_id)
    _push(portal, kind, record, "after-reconcile")

    _reconcile(portal, kind, removed=[record.id])
    page.wait_for_selector(f"#{row_id}", state="detached")
    assert page.evaluate(_ROW_STATE, row_id)["count"] == 0
    assert page.evaluate("document.querySelectorAll('tr tr').length") == 0


# ── 3.5: filter change and Load more after updates ───────────────────────────


def _load_more(portal, kind, appended):
    from app.domain.pagination import KeysetCursor
    from app.presentation.pagination import encode_cursor

    cursor = encode_cursor(KeysetCursor(created_at=datetime.now(UTC), id=uuid4()))
    # The next page holds only `appended`; afterwards every row is listed (and found) again.
    if kind == "booking":
        listed, everything = portal.listed, [*portal.listed, appended]
        portal.bookings[appended.id] = appended
        url, target = f"/book/vm/rows?cursor={cursor}", "#bookings-list"
    else:
        listed = portal.environments
        everything = [*portal.environments, appended]
        url, target = f"/environments/rows?cursor={cursor}", "#environments-tbody"
    listed[:] = [appended]
    portal.page.evaluate(
        "([url, target]) => htmx.ajax('GET', url, {target, swap: 'beforeend'})",
        [url, target],
    )
    _settled(portal.page, f"{kind}-{appended.id}")
    listed[:] = everything


def _change_filter(portal, kind):
    url, section = (
        ("/book/vm/list?filter=all", "#bookings-section")
        if kind == "booking"
        else ("/environments/list?filter=all", "#environments-section")
    )
    portal.page.evaluate(
        "([url, target]) => htmx.ajax('GET', url, {target, swap: 'outerHTML'})",
        [url, section],
    )


def test_filter_change_and_load_more_after_live_updates(live_list):
    portal, kind, record = live_list
    page = portal.page
    _push(portal, kind, record, "pre-filter")

    _change_filter(portal, kind)
    # The swapped-in section opens its own stream; the replaced one's is closed.
    page.wait_for_function("window.sse.sources.length === 2")
    assert page.evaluate("window.sse.sources.map((s) => s.readyState)") == [2, 1]
    _settled(page, f"{kind}-{record.id}")
    _assert_single(page, f"{kind}-{record.id}")
    _push(portal, kind, record, "post-filter")

    if kind == "booking":
        appended = _booking(portal.user, status=BookingStatus.PROVISIONING)
    else:
        from app.presentation.routes.environments import _annotate
        from tests.test_environment_extend_ui import _env

        appended = _annotate(_env(BookingStatus.PROVISIONING))
    displayed = record
    _load_more(portal, kind, appended)
    _assert_single(page, f"{kind}-{appended.id}")
    _assert_single(page, f"{kind}-{displayed.id}")
    _push(portal, kind, appended, "loaded-row-update")
    _push(portal, kind, displayed, "first-page-update")


# ── 3.6: a RELEASED row takes no further updates ─────────────────────────────


def _release(kind, record):
    from app.presentation.routes.environments import _annotate

    if kind == "booking":
        record.status = BookingStatus.RELEASED
    else:
        for child in record.bookings:
            child.status = BookingStatus.RELEASED
        _annotate(record)


def test_released_row_ignores_later_updates(live_list):
    portal, kind, record = live_list
    page, row_id = portal.page, f"{kind}-{record.id}"
    _push(portal, kind, record, "still-live")
    _release(kind, record)
    _push(portal, kind, record, "now-released")
    assert page.locator(f"#{row_id}").get_attribute("sse-swap") is None

    before = page.locator(f"#{row_id}").evaluate("(row) => row.outerHTML")
    _rename(kind, record, "too-late")
    page.evaluate("(id) => { window.sse.inserted[id] = 0; }", row_id)
    portal.push(kind, record)
    assert page.evaluate("(id) => window.sse.inserted[id]", row_id) == 0
    assert page.locator(f"#{row_id}").evaluate("(row) => row.outerHTML") == before
    _assert_single(page, row_id)
