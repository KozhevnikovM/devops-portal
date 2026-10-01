"""Unit/API tests for the list-section fragments of the list pages (#494).

`GET /book/vm/list`, `/book/namespace/list` and `/environments/list` serve a filter change: the
complete list section (filters, first page, empty state, first Load more) and nothing else — no
form, no order-form catalog read. They push the page URL (query-only, so a reverse-proxy subpath
prefix survives) into history. The page routes always return the full page, whatever the headers.
Statement counts and response sizes against real SQL live in
tests/integration/test_list_filter_response_cost.py.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.booking_list import BookingListItem
from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import EnvironmentPage, KeysetCursor, KeysetPage
from app.presentation.pagination import encode_cursor

_CURSOR = KeysetCursor(
    created_at=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc), id=uuid4(),
)
_NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

# (page path, list-section fragment path, section id, form marker)
_PAGES = {
    "vm": ("/book/vm", "/book/vm/list", "bookings-section", 'id="booking-form-area"'),
    "namespace": ("/book/namespace", "/book/namespace/list", "bookings-section",
                  'id="booking-form-area"'),
    "environments": ("/environments", "/environments/list", "environments-section",
                     'id="environment-order-form"'),
}


def _user(role="user", username="me"):
    return User(id=uuid4(), username=username, password_hash="", role=role,
                is_active=True, created_at=_NOW)


def _item(owner: User, *, resource_type=ResourceType.VM, status=BookingStatus.READY,
          has_credentials=False, label=None) -> BookingListItem:
    return BookingListItem(
        id=uuid4(), user_id=str(owner.id), status=status, resource_type=resource_type,
        ttl_minutes=240, expires_at=_NOW + timedelta(minutes=240), created_at=_NOW, label=label,
        status_message=None, config_failed=False, environment_id=None,
        owner_username=owner.username, created_by=None, created_by_username=None,
        image_id=None, image_name="ubuntu", hw_config_id=None, hw_config_name="small",
        vm_ip="10.0.0.1", namespace_name="ns-1", cluster_name="c1", api_url=None,
        static_vm_name=None, static_vm_host=None, static_vm_username=None,
        has_provisioning_log=False, config_role_names=(), has_credentials=has_credentials,
    )


def _env(owner: User, name="dev") -> Environment:
    return Environment(
        id=uuid4(), name=name, blueprint_name="dev", user_id=str(owner.id),
        ttl_minutes=240, expires_at=_NOW + timedelta(minutes=240), created_at=_NOW,
        bookings=[], created_by=None, owner_username=owner.username,
    )


@pytest.fixture(params=["user", "admin"])
def user(request):
    return _user(role=request.param)


@pytest.fixture
def client(user):
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def repos():
    """Every repository the three pages read; the catalog mocks are listed for assertions."""
    with (
        patch("app.presentation.routes.bookings._repo") as booking_repo,
        patch("app.presentation.routes.bookings._image_repo") as img,
        patch("app.presentation.routes.bookings._hw_config_repo") as hw,
        patch("app.presentation.routes.bookings._namespace_repo") as ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as svm,
        patch("app.presentation.routes.bookings._role_repo") as role,
        patch("app.presentation.routes.environments._env_repo") as env_repo,
        patch("app.presentation.routes.environments._blueprint_repo") as bp,
        patch("app.presentation.routes.environments._namespace_repo") as env_ns,
    ):
        booking_repo.list_page = AsyncMock(return_value=KeysetPage())
        booking_repo.queue_position = AsyncMock(return_value=1)
        env_repo.list_page = AsyncMock(return_value=EnvironmentPage())
        for parent, name in [(img, "list_active"), (hw, "list_active"), (ns, "list_available"),
                             (svm, "list_available"), (role, "list_active"), (bp, "list_active"),
                             (env_ns, "list_available"),
                             (env_ns, "list_held_standalone_by_user")]:
            setattr(parent, name, AsyncMock(return_value=[]))
        catalogs = {
            "booking": [img.list_active, hw.list_active, ns.list_available,
                        svm.list_available, role.list_active],
            "environment": [bp.list_active, env_ns.list_available,
                            env_ns.list_held_standalone_by_user],
        }
        yield {"booking": booking_repo, "environment": env_repo, "catalogs": catalogs,
               "role": role.list_active}


def _catalogs_for(repos, page):
    return repos["catalogs"]["environment" if page == "environments" else "booking"]


def _set_page(repos, page, items, next_cursor=None):
    if page == "environments":
        repos["environment"].list_page.return_value = EnvironmentPage(
            items=items, next_cursor=next_cursor)
    else:
        repos["booking"].list_page.return_value = KeysetPage(items=items, next_cursor=next_cursor)


def _rows(page, owner, n):
    if page == "environments":
        return [_env(owner, name=f"env-{i}") for i in range(n)]
    rt = ResourceType.NAMESPACE if page == "namespace" else ResourceType.VM
    return [_item(owner, resource_type=rt) for _ in range(n)]


def _section(html: str, section_id: str) -> str:
    """The list section of a full page, as rendered."""
    start = html.index(f'<section id="{section_id}">')
    return html[start:html.index("</section>", start) + len("</section>")]


def _load_more_url(html: str) -> str:
    start = html.index('hx-get="', html.index("-load-more")) + len('hx-get="')
    return html[start:html.index('"', start)].replace("&amp;", "&")


all_pages = pytest.mark.parametrize("page", list(_PAGES))


# ── 5.1: no catalogs, section only, history URL, auth, schema ────────────────

@all_pages
def test_list_section_reads_no_catalogs(client, repos, page):
    _, list_path, _, _ = _PAGES[page]

    resp = client.get(f"{list_path}?filter=all&show_released=1&label=web")

    assert resp.status_code == 200
    for catalog in _catalogs_for(repos, page):
        catalog.assert_not_awaited()
    list_repo = repos["environment" if page == "environments" else "booking"]
    list_repo.list_page.assert_awaited_once()


@all_pages
def test_page_route_reads_catalogs(client, repos, user, page):
    """Control for the test above: the page route does read the catalogs."""
    page_path, _, _, _ = _PAGES[page]

    assert client.get(page_path).status_code == 200

    for catalog in _catalogs_for(repos, page):
        if catalog is repos["role"] and user.role != "admin":
            catalog.assert_not_awaited()  # the role picker is admin-only (#377)
        else:
            catalog.assert_awaited()


@all_pages
def test_list_section_is_the_section_only(client, repos, page):
    _, list_path, section_id, form_marker = _PAGES[page]

    html = client.get(list_path).text.strip()

    assert html.startswith(f'<section id="{section_id}">')
    assert html.endswith("</section>")
    assert html.count(f'id="{section_id}"') == 1
    assert form_marker not in html
    assert "<html" not in html and "<body" not in html


@all_pages
@pytest.mark.parametrize("query,expected", [
    ("", {"filter": ["mine"]}),
    ("?filter=all", {"filter": ["all"]}),
    ("?filter=mine&show_released=1&label=a %26 b/c", {
        "filter": ["mine"], "show_released": ["1"], "label": ["a & b/c"]}),
    ("?filter=all&show_released=false&label=", {"filter": ["all"]}),
])
def test_list_section_pushes_query_only_page_url(client, repos, page, query, expected):
    _, list_path, _, _ = _PAGES[page]

    push = client.get(list_path + query).headers["HX-Push-Url"]

    # Query-only: the browser keeps the page's path and any subpath prefix (design D2).
    assert push.startswith("?")
    assert parse_qs(push[1:]) == expected
    # Same canonical order as the Load more URLs: filter, show_released, label.
    assert list(parse_qs(push[1:])) == [k for k in ("filter", "show_released", "label")
                                        if k in expected]


@all_pages
def test_unauthenticated_list_section_is_refused_like_the_page(page):
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    page_path, list_path, _, _ = _PAGES[page]
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    try:
        with patch("app.infrastructure.auth.get_current_user", AsyncMock(return_value=None)):
            client = TestClient(app, follow_redirects=False)
            page_resp, list_resp = client.get(page_path), client.get(list_path)
    finally:
        app.dependency_overrides.clear()

    assert list_resp.status_code == page_resp.status_code == 302
    assert list_resp.headers["location"] == page_resp.headers["location"] == "/auth/login"


def test_list_sections_absent_from_openapi():
    from app.main import app
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    for _, list_path, _, _ in _PAGES.values():
        assert list_path not in paths


@all_pages
def test_filter_controls_target_the_list_section(client, repos, page):
    """4.1: every filter control swaps the whole section from the fragment, never via hx-select."""
    page_path, list_path, section_id, _ = _PAGES[page]

    section = _section(client.get(f"{page_path}?filter=all&label=x").text, section_id)

    controls = section[:section.index("<table")]  # the filter bar, before the table
    assert controls.count(f'hx-get="{list_path}?') == 4  # label box, Mine, All, Show released
    assert controls.count(f'hx-target="#{section_id}"') == 4
    assert controls.count('hx-swap="outerHTML"') == 4
    assert "hx-select" not in section
    assert "hx-push-url" not in section
    assert f'id="{"bookings" if page != "environments" else "environments"}-label-filter"' \
        in controls


# ── 5.2: the section is complete ─────────────────────────────────────────────

@all_pages
def test_section_with_more_pages_has_first_page_and_load_more(client, repos, user, page):
    _, list_path, _, _ = _PAGES[page]
    _set_page(repos, page, _rows(page, user, 3), next_cursor=_CURSOR)

    html = client.get(f"{list_path}?filter=all&show_released=1&label=web").text

    url = _load_more_url(html)
    assert urlparse(url).path == list_path.replace("/list", "/rows")
    assert parse_qs(urlparse(url).query) == {
        "cursor": [encode_cursor(_CURSOR)], "filter": ["all"], "show_released": ["1"],
        "label": ["web"],
    }
    assert html.count("-load-more\"") == 1


@pytest.mark.parametrize("page,message", [
    ("vm", "No vm bookings yet."),
    ("namespace", "No namespace bookings yet."),
    ("environments", "No environments yet"),
])
def test_empty_section_shows_empty_state(client, repos, page, message):
    _, list_path, _, _ = _PAGES[page]

    html = client.get(list_path).text

    assert message in html
    assert "-load-more" not in html
    # The empty section keeps the page's live-update subscription.
    assert 'sse-connect="/events/stream"' in html


@pytest.mark.parametrize("page", ["vm", "namespace"])
def test_empty_label_section_says_no_match(client, repos, page):
    _, list_path, _, _ = _PAGES[page]

    html = client.get(f"{list_path}?label=zzz").text

    assert "No bookings match “zzz”." in html
    assert "bookings yet" not in html
    assert "-load-more" not in html


@pytest.mark.parametrize("page", ["vm", "namespace"])
def test_sparse_label_section_offers_search_older(client, repos, user, page):
    _, list_path, _, _ = _PAGES[page]
    _set_page(repos, page, [], next_cursor=_CURSOR)

    html = client.get(f"{list_path}?label=zzz").text

    assert "among the most recent bookings" in html
    assert "Search older bookings" in html
    assert parse_qs(urlparse(_load_more_url(html)).query)["label"] == ["zzz"]


@all_pages
@pytest.mark.parametrize("query", ["", "?filter=all&show_released=1&label=web"])
def test_fragment_equals_the_page_section(client, repos, user, page, query):
    page_path, list_path, section_id, _ = _PAGES[page]
    _set_page(repos, page, _rows(page, user, 3), next_cursor=_CURSOR)

    page_section = _section(client.get(page_path + query).text, section_id)
    fragment = client.get(list_path + query).text.strip()

    assert fragment == page_section


# ── 5.3: page routes stay full pages under HTMX headers ──────────────────────

@pytest.mark.parametrize("path,form_marker,catalog_kind", [
    ("/", 'id="booking-form-area"', "booking"),
    ("/book/vm", 'id="booking-form-area"', "booking"),
    ("/book/namespace", 'id="booking-form-area"', "booking"),
    ("/environments", 'id="environment-order-form"', "environment"),
])
@pytest.mark.parametrize("headers", [
    {"HX-Request": "true"},
    {"HX-Request": "true", "HX-History-Restore-Request": "true"},
    {"HX-Request": "true", "HX-Target": "bookings-section"},
])
def test_page_route_ignores_htmx_headers(client, repos, path, form_marker, catalog_kind, headers):
    resp = client.get(f"{path}?filter=all", headers=headers)

    assert resp.status_code == 200
    assert "<html" in resp.text and form_marker in resp.text
    assert "HX-Push-Url" not in resp.headers
    repos["catalogs"][catalog_kind][0].assert_awaited()


# ── 5.4: a filter change after Load more restarts the list ───────────────────

@all_pages
@pytest.mark.parametrize("query,expected", [
    ("?filter=all", {"user_id": None, "label": None, "include_released": False}),
    ("?filter=mine&label=new", {"label": "new", "include_released": False}),
    ("?filter=mine&show_released=1", {"label": None, "include_released": True}),
])
def test_filter_change_after_load_more_restarts_then_continues(client, repos, user, page,
                                                               query, expected):
    _, list_path, _, _ = _PAGES[page]
    list_repo = repos["environment" if page == "environments" else "booking"]
    _set_page(repos, page, _rows(page, user, 2), next_cursor=_CURSOR)
    # Earlier pages were loaded; the filter change asks for the first page again.
    html = client.get(list_path + query).text

    first = list_repo.list_page.await_args.kwargs
    assert first["after"] is None
    for key, value in expected.items():
        assert first[key] == value
    if "user_id" not in expected:
        assert first["user_id"] == str(user.id)
    assert html.count("-load-more\"") == 1

    # Load more from the new section continues with the new filters.
    client.get(_load_more_url(html))
    nxt = list_repo.list_page.await_args.kwargs
    assert nxt["after"] == _CURSOR
    assert {k: nxt[k] for k in ("user_id", "label", "include_released")} == \
        {k: first[k] for k in ("user_id", "label", "include_released")}


# ── 5.5: credentials and permission gating are unchanged ─────────────────────

def _row(html: str, row_id) -> str:
    start = html.index(f'id="booking-{row_id}"')
    return html[start:html.index("</tr>", start)]


@pytest.mark.parametrize("page", ["vm", "namespace"])
def test_credentials_control_follows_ownership(client, repos, user, page):
    other = _user(username="someone-else")
    rt = ResourceType.NAMESPACE if page == "namespace" else ResourceType.VM
    mine = _item(user, resource_type=rt, has_credentials=True)
    theirs = _item(other, resource_type=rt, has_credentials=True)
    _set_page(repos, page, [mine, theirs])

    html = client.get(f"{_PAGES[page][1]}?filter=all").text

    assert "Show credentials" in _row(html, mine.id)
    # Only the owner or an admin may reveal credentials (#478).
    assert ("Show credentials" in _row(html, theirs.id)) == (user.role == "admin")
    # The list never carries credential values, only the presence flag.
    assert "vm_password" not in html and "ssh_key" not in html


def test_all_list_actions_follow_role(client, repos, user):
    other = _user(username="someone-else")
    theirs = _item(other, status=BookingStatus.READY)
    _set_page(repos, "vm", [theirs])

    html = client.get("/book/vm/list?filter=all").text

    can_release = f'hx-delete="/bookings/{theirs.id}"' in _row(html, theirs.id)
    assert can_release == (user.role == "admin")


def test_environment_all_list_actions_follow_role(client, repos, user):
    other = _user(username="someone-else")
    mine, theirs = _env(user, "mine"), _env(other, "theirs")
    _set_page(repos, "environments", [mine, theirs])

    html = client.get("/environments/list?filter=all").text

    assert f'hx-patch="/environments/{mine.id}/name"' in html
    assert (f'hx-patch="/environments/{theirs.id}/name"' in html) == (user.role == "admin")
    assert (f'hx-delete="/environments/{theirs.id}"' in html) == (user.role == "admin")
