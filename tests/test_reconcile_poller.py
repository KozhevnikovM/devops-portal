"""Each list section carries exactly one reconciliation poller and a newer-rows indicator (#497 D6).

Load more pages, order responses and single rows carry none, so however many pages are loaded
there is one timer per section; the poller's URL carries the filters in effect.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from app.config import settings
from app.domain.pagination import KeysetCursor
from app.presentation.pagination import encode_cursor
from tests.test_list_section_fragment import (  # noqa: F401 — fixtures
    _PAGES, _env, _item, _rows, _set_page, client, repos, user,
)

_POLLER = {"vm": "bookings-reconcile", "namespace": "bookings-reconcile",
           "environments": "environments-reconcile"}
_INDICATOR = {"vm": "bookings-new-rows", "namespace": "bookings-new-rows",
              "environments": "environments-new-rows"}
_CURSOR = encode_cursor(KeysetCursor(created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), id=uuid4()))
all_pages = pytest.mark.parametrize("page", sorted(_PAGES))


def _poller(html: str, page: str) -> str:
    start = html.index(f'id="{_POLLER[page]}"')
    return html[html.rindex("<div", 0, start):html.index(">", start)]


def _attr(tag: str, name: str) -> str:
    start = tag.index(f'{name}="') + len(name) + 2
    return tag[start:tag.index('"', start)]


@all_pages
def test_page_and_list_section_carry_one_poller(client, repos, user, page):
    page_path, list_path, _, _ = _PAGES[page]
    _set_page(repos, page, _rows(page, user, 3), next_cursor=KeysetCursor(
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), id=uuid4()))
    for path in (page_path, list_path):
        html = client.get(path).text
        assert html.count(f'id="{_POLLER[page]}"') == 1
        assert html.count(f'id="{_INDICATOR[page]}"') == 1
        poller = _poller(html, page)
        assert 'hx-trigger="every 60s"' in poller and 'hx-swap="none"' in poller
        assert 'hx-sync="this:drop"' in poller
        assert _attr(poller, "data-reconcile-max") == str(settings.RECONCILE_MAX_IDS)
        assert _attr(poller, "data-reconcile-settled-min") == str(settings.RECONCILE_SETTLED_MIN)


@all_pages
def test_load_more_pages_carry_no_poller(client, repos, user, page):
    page_path, _, _, _ = _PAGES[page]
    _set_page(repos, page, _rows(page, user, 3), next_cursor=KeysetCursor(
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), id=uuid4()))
    rows_path = "/environments/rows" if page == "environments" else f"{page_path}/rows"
    first = client.get(page_path).text
    appended = [client.get(f"{rows_path}?cursor={_CURSOR}").text for _ in range(2)]
    for html in appended:
        assert "-reconcile" not in html and "-new-rows" not in html
    assert (first + "".join(appended)).count(f'id="{_POLLER[page]}"') == 1


@all_pages
@pytest.mark.parametrize("query", ["filter=all", "filter=mine&show_released=1&label=a%20b"])
def test_poller_url_carries_the_filters(client, repos, user, page, query):
    _, list_path, _, _ = _PAGES[page]
    html = client.get(f"{list_path}?{query}").text
    url = urlparse(_attr(_poller(html, page), "hx-get").replace("&amp;", "&"))
    expected_path = "/environments/reconcile" if page == "environments" else list_path.replace("/list", "/reconcile")
    assert url.path == expected_path
    assert parse_qs(url.query) == parse_qs(query)


def test_empty_environments_section_still_reconciles(client, repos):
    html = client.get("/environments/list").text
    assert "No environments yet" in html
    assert html.count('id="environments-reconcile"') == 1
    assert html.count('id="environments-new-rows"') == 1


def test_single_rows_and_order_responses_carry_no_poller(client, repos, user):
    booking = _item(user)
    env = _env(user)
    with patch("app.presentation.routes.environments._order_use_case") as order:
        order.execute = AsyncMock(return_value=env)
        repos["booking"].get = AsyncMock(return_value=booking)
        repos["environment"].get = AsyncMock(return_value=env)
        responses = [
            client.get(f"/bookings/{booking.id}/row").text,
            client.get(f"/environments/{env.id}/row").text,
            client.post("/environments", data={"blueprint_name": "dev", "ttl_minutes": "240"}).text,
        ]
    for html in responses:
        assert "-reconcile" not in html and "-new-rows" not in html


@all_pages
def test_indicator_is_empty_until_reconciliation_reports_newer_rows(client, repos, user, page):
    _, list_path, _, _ = _PAGES[page]
    html = client.get(list_path).text
    start = html.index(f'<tr id="{_INDICATOR[page]}"')
    assert html[start:html.index("</tr>", start)].count("<td") == 0
