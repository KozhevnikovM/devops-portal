"""loadtest/reconcile.py (#505 D7): a simulated tab replays the reconcile poll the way the browser does.

The module is loaded by file path and never imports Locust. Pages are rendered by the app itself
(the list-section tests' repository mocks), so a template change that the load test's parser
can't follow fails here, in the fast suite, before it silently drops load-test traffic.
"""

import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.config import settings
from app.domain.enums import BookingStatus as S
from app.presentation.reconcile import list_key, row_version
from tests import test_list_section_fragment as fragment
from tests.reconcile_oracle import select_batch

client, repos, user = fragment.client, fragment.repos, fragment.user
_item, _set_page = fragment._item, fragment._set_page

_PATH = Path(__file__).resolve().parent.parent / "loadtest" / "reconcile.py"
_spec = importlib.util.spec_from_file_location("loadtest_reconcile", _PATH)
lt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = (
    lt  # dataclasses look their module up while the class is built
)
_spec.loader.exec_module(lt)

_VERSION = "0123456789abcdef"


def _query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


def _page(client, repos, user, statuses):
    items = [_item(user, status=status) for status in statuses]
    _set_page(repos, "vm", items)
    return items, client.get("/?filter=all").text


def _synthetic(n_inflight, n_settled, max_ids=4, settled_min=1):
    rows = [
        lt.Row(f"booking-i{k}", _VERSION, "inflight", f"key-i{k}")
        for k in range(n_inflight)
    ]
    rows += [
        lt.Row(f"booking-s{k}", _VERSION, "settled", f"key-s{k}")
        for k in range(n_settled)
    ]
    return lt.Section(
        url="/book/vm/reconcile?filter=all",
        max_ids=max_ids,
        settled_min=settled_min,
        rows=rows,
    )


# ── Parsing a rendered page ───────────────────────────────────────────────────


def test_rows_parsed_from_a_rendered_page(client, repos, user):
    items, html = _page(
        client, repos, user, [S.PROVISIONING, S.READY, S.RELEASED, S.FAILED]
    )
    section = lt.parse_section(html)

    assert section.url == "/book/vm/reconcile?filter=all"
    assert section.stats_name == "/book/vm/reconcile"
    assert section.max_ids == settings.RECONCILE_MAX_IDS
    assert section.settled_min == settings.RECONCILE_SETTLED_MIN
    assert [(r.dom_id, r.version, r.live, r.key) for r in section.rows] == [
        (
            f"booking-{items[0].id}",
            row_version(items[0]),
            "inflight",
            list_key(items[0]),
        ),
        (
            f"booking-{items[1].id}",
            row_version(items[1]),
            "settled",
            list_key(items[1]),
        ),
        (
            f"booking-{items[2].id}",
            None,
            None,
            list_key(items[2]),
        ),  # RELEASED: not live
        (
            f"booking-{items[3].id}",
            row_version(items[3]),
            "settled",
            list_key(items[3]),
        ),
    ]


def test_environments_page_parsed(client, repos, user):
    envs = [fragment._env(user, name="a"), fragment._env(user, name="b")]
    _set_page(repos, "environments", envs)
    section = lt.parse_section(client.get("/environments?filter=mine").text)
    assert section.stats_name == "/environments/reconcile"
    assert [r.row_id for r in section.rows] == [str(e.id) for e in envs]


@pytest.mark.parametrize(
    "html, missing",
    [
        (
            "<section><table><tbody id='bookings-list'></tbody></table></section>",
            "no reconcile poller",
        ),
        (
            '<div data-reconcile-rows="#x" hx-get="/r" data-reconcile-max="5"></div>',
            "data-reconcile-settled-min",
        ),
        (
            '<div data-reconcile-rows="#x" data-reconcile-max="5" data-reconcile-settled-min="1"></div>',
            "hx-get",
        ),
    ],
)
def test_missing_poller_or_attribute_is_template_drift(html, missing):
    with pytest.raises(lt.ReconcileMarkupError, match=missing):
        lt.parse_section(html)


# ── The request a poll sends ──────────────────────────────────────────────────


def test_populated_page_sends_r_per_batch_row_and_newest(client, repos, user):
    items, html = _page(client, repos, user, [S.PROVISIONING, S.READY, S.RELEASED])
    query = _query(lt.parse_section(html).next_url())

    assert query[0] == ("filter", "all")
    assert [v for k, v in query if k == "r"] == [
        f"{items[0].id}.{row_version(items[0])}",
        f"{items[1].id}.{row_version(items[1])}",
    ]
    assert [v for k, v in query if k == "newest"] == [list_key(items[0])]


def test_released_first_row_still_defines_newest(client, repos, user):
    items, html = _page(client, repos, user, [S.RELEASED, S.READY])
    query = _query(lt.parse_section(html).next_url())
    assert ("newest", list_key(items[0])) in query
    assert [v for k, v in query if k == "r"] == [
        f"{items[1].id}.{row_version(items[1])}"
    ]


def test_empty_page_sends_neither_r_nor_newest(client, repos, user):
    _, html = _page(client, repos, user, [])
    section = lt.parse_section(html)
    assert not any(r.live or r.key for r in section.rows)  # at most the empty-state row
    assert section.next_url() == "/book/vm/reconcile?filter=all"


def test_batch_capped_at_the_advertised_maximum():
    section = _synthetic(n_inflight=10, n_settled=10, max_ids=4, settled_min=1)
    rs = [v for k, v in _query(section.next_url()) if k == "r"]
    assert len(rs) == 4
    assert [r.split(".")[0] for r in rs] == [
        "i0",
        "i1",
        "i2",
        "s0",
    ]  # in-flight first, one settled slot


def test_rotation_across_polls_matches_select_batch():
    section = _synthetic(n_inflight=5, n_settled=3, max_ids=3, settled_min=1)
    inflight = [r for r in section.rows if r.live == "inflight"]
    settled = [r for r in section.rows if r.live == "settled"]
    offsets, named = {}, set()
    for _ in range(6):
        expected, offsets = select_batch(inflight, settled, 3, 1, offsets)
        rs = [v for k, v in _query(section.next_url()) if k == "r"]
        assert rs == [f"{r.row_id}.{r.version}" for r in expected]
        named.update(rs)
    assert len(named) == len(section.rows)  # every displayed row is named over time


def test_reload_resets_rotation(client, repos, user):
    _, html = _page(client, repos, user, [S.READY] * (settings.RECONCILE_MAX_IDS + 2))
    first = lt.parse_section(html)
    initial = first.next_url()
    assert first.next_url() != initial  # rotated
    assert (
        lt.parse_section(html).next_url() == initial
    )  # a reloaded section starts over


# ── Applying the response ─────────────────────────────────────────────────────


def test_versions_updated_and_deleted_rows_dropped_from_a_response_fragment():
    section = _synthetic(n_inflight=2, n_settled=1)
    response = (
        '<tr id="bookings-new-rows" hx-swap-oob="true"></tr>'
        '<tr id="booking-i0" data-key="key-i0" hx-swap-oob="true" sse-swap="booking-i0"'
        ' data-row-version="fedcba9876543210" data-live="settled"><td>x</td></tr>'
        '<tr id="booking-s0" data-key="key-s0" hx-swap-oob="true"><td>released</td></tr>'
        '<tr id="booking-i1" hx-swap-oob="delete"></tr>'
        '<tr id="booking-unknown" hx-swap-oob="delete"></tr>'
    )
    section.apply_response(response)

    assert [(r.dom_id, r.version, r.live) for r in section.rows] == [
        ("booking-i0", "fedcba9876543210", "settled"),
        ("booking-s0", None, None),
    ]
    assert [v for k, v in _query(section.next_url()) if k == "r"] == [
        "i0.fedcba9876543210"
    ]
