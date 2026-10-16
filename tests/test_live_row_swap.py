"""Every row that takes live (SSE) updates replaces itself whole (#541).

htmx-sse swaps an event into its `sse-swap` element with that element's `hx-swap`, falling back to
`innerHTML`. The event data is a complete `<tr>`, so without `hx-swap="outerHTML"` it lands
*inside* the old row: nested rows and a duplicate id. `settle:0ms` makes htmx process the new row
(and so register its live listener) during the swap rather than 20ms later, when an update right
behind this one would already have been dropped. `hx-disinherit="hx-swap"` keeps that swap
from leaking into the row's own action controls. The real-browser checks are in
tests/test_live_row_swap_browser.py.
"""

from html.parser import HTMLParser

import pytest

from app.domain.enums import BookingStatus as S
from tests.test_row_version import _booking, _child, _env, _user


class _Elements(HTMLParser):
    """The attributes of every start tag in a document, in order."""

    def __init__(self, html):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _live_elements(html):
    return [(tag, attrs) for tag, attrs in _Elements(html).tags if "sse-swap" in attrs]


def _assert_replaces_itself(tag, attrs):
    assert tag == "tr", attrs
    # settle:0ms: the swapped-in row registers its own live listener before the next event.
    assert attrs.get("hx-swap", "").split() == ["outerHTML", "settle:0ms"], attrs
    assert "hx-swap" in attrs.get("hx-disinherit", "").split(), attrs


def _render(name, **context):
    from app.presentation.templating import templates

    return templates.get_template(f"partials/{name}").render(**context)


@pytest.mark.parametrize(
    "status", [S.QUEUED, S.PROVISIONING, S.READY, S.FAILED, S.RELEASING]
)
def test_live_booking_row_replaces_itself(status):
    owner = _user()
    booking = _booking(owner, status=status)
    html = _render("booking_row.html", booking=booking, current_user=owner)
    live = _live_elements(html)
    assert [attrs["sse-swap"] for _, attrs in live] == [f"booking-{booking.id}"]
    _assert_replaces_itself(*live[0])


@pytest.mark.parametrize("status", [S.PROVISIONING, S.READY, S.FAILED])
def test_live_environment_row_replaces_itself(status):
    owner = _user()
    env = _env(owner, children=[_child(status)])
    html = _render("environment_row.html", environment=env, current_user=owner)
    live = _live_elements(html)
    assert [attrs["sse-swap"] for _, attrs in live] == [f"environment-{env.id}"]
    _assert_replaces_itself(*live[0])


def test_released_rows_take_no_live_updates():
    owner = _user()
    booking = _booking(owner, status=S.RELEASED)
    env = _env(owner, children=[_child(S.RELEASED)])
    assert not _live_elements(
        _render("booking_row.html", booking=booking, current_user=owner)
    )
    assert not _live_elements(
        _render("environment_row.html", environment=env, current_user=owner)
    )


def test_environment_reconcile_rows_replace_themselves():
    """Both the re-rendered rows and the over-child-limit placeholder take live updates."""
    owner = _user()
    changed = _env(owner, children=[_child(S.PROVISIONING)])
    over_limit = _env(owner)
    html = _render(
        "environment_reconcile.html",
        current_user=owner,
        changed=[changed],
        over_limit=[over_limit],
        removed=[],
        new_rows_id="environments-new-rows",
    )
    live = _live_elements(html)
    assert sorted(attrs["sse-swap"] for _, attrs in live) == sorted(
        [f"environment-{changed.id}", f"environment-{over_limit.id}"]
    )
    for tag, attrs in live:
        _assert_replaces_itself(tag, attrs)


def test_row_controls_do_not_inherit_the_live_swap():
    """Every row control that issues a request names its own swap, so disinherit is a safeguard."""
    owner = _user()
    for html in (
        _render(
            "booking_row.html",
            booking=_booking(owner, status=S.READY),
            current_user=owner,
        ),
        _render(
            "environment_row.html",
            environment=_env(owner),
            current_user=owner,
        ),
    ):
        for _, attrs in _Elements(html).tags:
            if any(
                k in attrs
                for k in ("hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")
            ):
                assert "hx-swap" in attrs, attrs
