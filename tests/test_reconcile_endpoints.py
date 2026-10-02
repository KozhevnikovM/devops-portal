"""Unit/API tests for page row reconciliation (#497): GET /book/vm/reconcile,
/book/namespace/reconcile and /environments/reconcile.

One bounded request per list section per interval: the server validates it before reading
anything, re-authorizes every id by list visibility (the batch read is scoped by the page's kinds
and Mine/All), answers with out-of-band updates of changed rows only, removal directives that look
the same for unknown and invisible ids, and the newer-rows indicator. Statement counts against
real SQL live in tests/integration/test_reconcile_cost.py.
"""
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.domain.booking_list import BookingListItem, EnvironmentChildItem
from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import KeysetCursor
from app.presentation.pagination import encode_cursor
from app.presentation.reconcile import environment_row_version, list_key, row_version

S = BookingStatus
_NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
_BOOKING_PAGES = {"vm": ("/book/vm", ResourceType.VM, ["VM", "STATIC_VM"]),
                  "namespace": ("/book/namespace", ResourceType.NAMESPACE, ["NAMESPACE"])}
_ALL_PATHS = ["/book/vm/reconcile", "/book/namespace/reconcile", "/environments/reconcile"]


def _user(role="user", username="me"):
    return User(id=uuid4(), username=username, password_hash="", role=role, is_active=True,
                created_at=_NOW)


def _item(owner: User, *, rt=ResourceType.VM, status=S.PROVISIONING, minutes=0, **kw) -> BookingListItem:
    base = {
        "id": uuid4(), "user_id": str(owner.id), "status": status, "resource_type": rt, "ttl_minutes": 240,
        "expires_at": _NOW + timedelta(minutes=240), "created_at": _NOW - timedelta(minutes=minutes),
        "label": None, "status_message": None, "config_failed": False, "environment_id": None,
        "owner_username": owner.username, "created_by": None, "created_by_username": None, "image_id": None,
        "image_name": "ubuntu", "hw_config_id": None, "hw_config_name": "small", "vm_ip": "10.0.0.1",
        "namespace_name": "ns-1", "cluster_name": "c1", "api_url": None, "static_vm_name": None,
        "static_vm_host": None, "static_vm_username": None, "has_provisioning_log": False,
        "config_role_names": (), "has_credentials": False,
    }
    base.update(kw)
    return BookingListItem(**base)


def _child(status=S.READY) -> EnvironmentChildItem:
    return EnvironmentChildItem(
        id=uuid4(), status=status, resource_type=ResourceType.NAMESPACE, created_at=_NOW,
        environment_label="ns", namespace_name="ns-1", static_vm_name=None, static_vm_host=None,
        image_name=None, vm_ip=None, config_failed=False,
    )


def _env(owner: User, children=None, minutes=0, **kw) -> Environment:
    base = {"id": uuid4(), "name": "dev", "blueprint_name": "dev", "user_id": str(owner.id), "ttl_minutes": 240,
                "expires_at": _NOW + timedelta(minutes=240), "created_at": _NOW - timedelta(minutes=minutes),
                "bookings": children if children is not None else [_child(S.PROVISIONING)],
                "owner_username": owner.username}
    base.update(kw)
    return Environment(**base)


def _env_version(env: Environment) -> str:
    from app.presentation.routes.environments import _annotate
    return environment_row_version(_annotate(replace(env)))


def _token(row_id, version="0" * 16):
    return f"{row_id}.{version}"


@pytest.fixture
def user():
    return _user()


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
    with (
        patch("app.presentation.routes.bookings._repo") as booking_repo,
        patch("app.presentation.routes.environments._env_repo") as env_repo,
        patch("app.presentation.routes.bookings._image_repo") as img,
        patch("app.presentation.routes.bookings._hw_config_repo") as hw,
        patch("app.presentation.routes.bookings._namespace_repo") as ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as svm,
        patch("app.presentation.routes.bookings._role_repo") as role,
        patch("app.presentation.routes.environments._blueprint_repo") as bp,
        patch("app.presentation.routes.environments._namespace_repo") as env_ns,
    ):
        booking_repo.list_items_by_ids = AsyncMock(return_value=[])
        booking_repo.newest_key = AsyncMock(return_value=None)
        booking_repo.queue_positions = AsyncMock(side_effect=lambda session, rows: {b.id: 5 for b in rows})
        env_repo.list_items_by_ids = AsyncMock(return_value=([], []))
        env_repo.newest_key = AsyncMock(return_value=None)
        catalogs = []
        for parent, name in [(img, "list_active"), (hw, "list_active"), (ns, "list_available"),
                             (svm, "list_available"), (role, "list_active"), (bp, "list_active"),
                             (env_ns, "list_available"), (env_ns, "list_held_standalone_by_user")]:
            setattr(parent, name, AsyncMock(return_value=[]))
            catalogs.append(getattr(parent, name))
        yield {"booking": booking_repo, "environment": env_repo, "catalogs": catalogs}


def _oob_rows(html: str) -> dict[str, str]:
    """id → hx-swap-oob value for every top-level row of a reconcile response."""
    return dict(re.findall(r'<tr\s+id="([^"]+)"[^>]*?hx-swap-oob="([^"]+)"', html, flags=re.DOTALL))


booking_pages = pytest.mark.parametrize("page", sorted(_BOOKING_PAGES))


# ── 5.1: bounds, before any read ──────────────────────────────────────────────
@pytest.mark.parametrize("path", _ALL_PATHS)
@pytest.mark.parametrize("query", [
    "&".join(f"r={_token(uuid4())}" for _ in range(settings.RECONCILE_MAX_IDS + 1)),
    "r={0}&r={0}".format(_token("11111111-1111-1111-1111-111111111111")),
    "r=not-a-uuid.0000000000000000",
    f"r={uuid4()}",                       # no version
    f"r={uuid4()}.XYZ0000000000000",      # not lowercase hex
    f"r={uuid4()}.00000000",              # too short
    f"r={uuid4()}.{'0' * 17}",            # too long
    "newest=!!not-a-cursor",
])
def test_invalid_request_is_rejected_before_any_read(client, repos, path, query):
    resp = client.get(f"{path}?filter=mine&{query}")
    assert resp.status_code == 400
    for repo in (repos["booking"], repos["environment"]):
        repo.list_items_by_ids.assert_not_called()
        repo.newest_key.assert_not_called()
    repos["booking"].queue_positions.assert_not_called()


@pytest.mark.parametrize("path", _ALL_PATHS)
def test_request_at_the_cap_is_accepted(client, repos, path):
    query = "&".join(f"r={_token(uuid4())}" for _ in range(settings.RECONCILE_MAX_IDS))
    assert client.get(f"{path}?{query}").status_code == 200


# ── 5.2: bookings ─────────────────────────────────────────────────────────────
@booking_pages
@pytest.mark.parametrize("filter, mine", [("mine", True), ("all", False)])
def test_batch_read_is_scoped_to_the_page_and_filter(client, repos, user, page, filter, mine):
    path, _, types = _BOOKING_PAGES[page]
    ids = [uuid4(), uuid4()]
    client.get(f"{path}/reconcile?filter={filter}&" + "&".join(f"r={_token(i)}" for i in ids))
    call = repos["booking"].list_items_by_ids.call_args
    assert call.args[1] == ids
    assert call.kwargs == {"user_id": str(user.id) if mine else None, "resource_types": types}
    probe = repos["booking"].newest_key.call_args.kwargs
    assert probe["user_id"] == (str(user.id) if mine else None)
    assert probe["resource_types"] == types


@booking_pages
def test_response_starts_with_the_indicator_row(client, repos, page):
    path, _, _ = _BOOKING_PAGES[page]
    html = client.get(f"{path}/reconcile").text
    assert html.lstrip().startswith('<tr id="bookings-new-rows" hx-swap-oob="true">')


@booking_pages
def test_only_changed_rows_are_rendered(client, repos, user, page):
    path, rt, _ = _BOOKING_PAGES[page]
    same, changed = _item(user, rt=rt), _item(user, rt=rt, status=S.READY)
    repos["booking"].list_items_by_ids.return_value = [same, changed]
    html = client.get(f"{path}/reconcile?r={_token(same.id, row_version(same))}"
                      f"&r={_token(changed.id)}").text
    rows = _oob_rows(html)
    assert f"booking-{same.id}" not in rows
    assert rows[f"booking-{changed.id}"] == "true"
    assert f'data-row-version="{row_version(changed)}"' in html


@booking_pages
def test_queue_position_change_is_a_change(client, repos, user, page):
    path, rt, _ = _BOOKING_PAGES[page]
    queued = _item(user, rt=rt, status=S.QUEUED)
    repos["booking"].list_items_by_ids.return_value = [queued]
    # The page holds the version for position 3; the rank read now says 5.
    html = client.get(f"{path}/reconcile?r={_token(queued.id, row_version(replace(queued, queue_position=3)))}").text
    assert _oob_rows(html)[f"booking-{queued.id}"] == "true"
    assert "Queued — position 5" in html


@booking_pages
def test_released_row_is_rendered_final(client, repos, user, page):
    path, rt, _ = _BOOKING_PAGES[page]
    released = _item(user, rt=rt, status=S.RELEASED)
    repos["booking"].list_items_by_ids.return_value = [released]
    html = client.get(f"{path}/reconcile?show_released=0&r={_token(released.id)}").text
    tr = html[html.index(f'id="booking-{released.id}"'):]
    tr = tr[:tr.index(">")]
    assert 'hx-swap-oob="true"' in tr and "data-live" not in tr and "sse-swap" not in tr
    assert f'data-key="{list_key(released)}"' in tr


@booking_pages
def test_forged_and_unknown_ids_get_identical_directives(client, repos, user, page):
    path, _rt, _ = _BOOKING_PAGES[page]
    forged, unknown = uuid4(), uuid4()   # the scoped read returns neither
    html = client.get(f"{path}/reconcile?r={_token(forged)}&r={_token(unknown)}").text
    directive = '<tr id="booking-{}" hx-swap-oob="delete"></tr>'
    assert directive.format(forged) in html and directive.format(unknown) in html
    assert html.replace(str(forged), "X").count(directive.format("X")) == 1
    assert html.count("hx-swap-oob") == 3   # indicator + two removals, nothing else


@pytest.mark.parametrize("role, manages", [("user", False), ("admin", True)])
def test_foreign_rows_on_all_follow_the_list_gating(repos, role, manages):
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    viewer, owner = _user(role, "viewer"), _user(username="owner")
    foreign = _item(owner, status=S.READY, has_credentials=True)
    repos["booking"].list_items_by_ids.return_value = [foreign]
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: viewer
    try:
        html = TestClient(app).get(f"/book/vm/reconcile?filter=all&r={_token(foreign.id)}").text
    finally:
        app.dependency_overrides.clear()
    assert f'id="booking-{foreign.id}"' in html
    assert ("Show credentials" in html) is manages
    assert (f'hx-delete="/bookings/{foreign.id}"' in html) is manages
    assert "s3cret" not in html


@pytest.mark.parametrize("path", _ALL_PATHS)
def test_unauthenticated_reconcile_is_refused_like_the_page(path):
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    try:
        with patch("app.infrastructure.auth.get_current_user", AsyncMock(return_value=None)):
            resp = TestClient(app, follow_redirects=False).get(path)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 302 and resp.headers["location"] == "/auth/login"


def test_reconcile_routes_absent_from_openapi():
    from app.main import app
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    for path in _ALL_PATHS:
        assert path not in paths


@pytest.mark.parametrize("path", _ALL_PATHS)
def test_no_order_form_catalog_is_read(client, repos, path):
    client.get(f"{path}?r={_token(uuid4())}")
    for catalog in repos["catalogs"]:
        catalog.assert_not_called()


@booking_pages
def test_one_read_of_each_kind_whatever_the_batch_size(client, repos, user, page):
    path, rt, _ = _BOOKING_PAGES[page]
    items = [_item(user, rt=rt, status=S.QUEUED) for _ in range(settings.RECONCILE_MAX_IDS)]
    repos["booking"].list_items_by_ids.return_value = items
    client.get(f"{path}/reconcile?" + "&".join(f"r={_token(b.id)}" for b in items))
    assert repos["booking"].list_items_by_ids.await_count == 1
    assert repos["booking"].queue_positions.await_count == 1
    assert repos["booking"].newest_key.await_count == 1


# ── newer-rows indicator, and the list key it compares (#497 D8) ──────────────
def _key(minutes):
    return KeysetCursor(created_at=_NOW - timedelta(minutes=minutes), id=uuid4())


@pytest.mark.parametrize("path, repo", [("/book/vm/reconcile", "booking"),
                                        ("/environments/reconcile", "environment")])
@pytest.mark.parametrize("probe, newest, shown", [
    (None, None, False),                 # nothing matches at all
    ("p", None, True),                   # empty section, a matching row exists
    ("older", "newer", False),           # the newest displayed row is the newest match
    ("same", "same", False),
    ("newer", "older", True),            # ordered elsewhere since the page rendered
])
def test_newer_rows_indicator(client, repos, path, repo, probe, newest, shown):
    keys = {"p": _key(0), "older": _key(10), "newer": _key(1)}
    keys["same"] = keys["p"]
    repos[repo].newest_key.return_value = keys.get(probe)
    query = f"&newest={encode_cursor(keys[newest])}" if newest else ""
    html = client.get(f"{path}?filter=mine{query}").text
    assert ("refresh list" in html) is shown
    if shown:
        assert 'hx-swap="outerHTML"' in html and "/list?filter=mine" in html


def test_released_first_row_still_defines_newest(client, repos, user):
    # Show released on, the first displayed row is RELEASED and nothing newer exists: its key is
    # still the newest displayed key, so no false indicator.
    released = _item(user, status=S.RELEASED)
    key = KeysetCursor(created_at=released.created_at, id=released.id)
    repos["booking"].newest_key.return_value = key
    html = client.get(f"/book/vm/reconcile?show_released=1&newest={list_key(released)}").text
    assert "refresh list" not in html


def test_first_row_rendered_released_keeps_its_key(client, repos, user):
    # Reconciliation renders the first row RELEASED: it keeps data-key, so the next request still
    # sends it as the newest displayed key.
    released = _item(user, status=S.RELEASED)
    repos["booking"].list_items_by_ids.return_value = [released]
    repos["booking"].newest_key.return_value = KeysetCursor(created_at=released.created_at, id=released.id)
    html = client.get(f"/book/vm/reconcile?newest={list_key(released)}&r={_token(released.id)}").text
    assert f'data-key="{list_key(released)}"' in html
    assert "refresh list" not in html


# ── 5.3: environments ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("filter, mine", [("mine", True), ("all", False)])
def test_environment_batch_read_is_scoped(client, repos, user, filter, mine):
    ids = [uuid4()]
    client.get(f"/environments/reconcile?filter={filter}&r={_token(ids[0])}")
    call = repos["environment"].list_items_by_ids.call_args
    assert call.args[1] == ids
    assert call.kwargs["user_id"] == (str(user.id) if mine else None)
    assert call.kwargs["child_limit"] == settings.ENVIRONMENT_MAX_CHILDREN


def test_environment_child_limit_comes_from_startup(client, repos):
    from app.main import app
    app.state.environment_child_limit = 31
    try:
        client.get(f"/environments/reconcile?r={_token(uuid4())}")
    finally:
        del app.state.environment_child_limit
    assert repos["environment"].list_items_by_ids.call_args.kwargs["child_limit"] == 31


def test_environment_child_status_change_is_returned(client, repos, user):
    before = _env(user, children=[_child(S.READY)])
    after = replace(before, bookings=[replace(before.bookings[0], status=S.RELEASING)])
    repos["environment"].list_items_by_ids.return_value = ([after], [])
    html = client.get(f"/environments/reconcile?r={_token(before.id, _env_version(before))}").text
    assert _oob_rows(html)[f"environment-{before.id}"] == "true"
    assert "RELEASING" in html


def test_unchanged_environment_is_not_rendered(client, repos, user):
    env = _env(user)
    repos["environment"].list_items_by_ids.return_value = ([env], [])
    html = client.get(f"/environments/reconcile?r={_token(env.id, _env_version(env))}").text
    assert f"environment-{env.id}" not in html


def test_environment_at_the_child_limit_is_rendered_in_full(client, repos, user):
    children = [_child(S.READY) for _ in range(settings.ENVIRONMENT_MAX_CHILDREN)]
    env = _env(user, children=children)
    repos["environment"].list_items_by_ids.return_value = ([env], [])
    html = client.get(f"/environments/reconcile?r={_token(env.id)}").text
    assert html.count("status-READY") >= len(children)


def test_environment_over_the_limit_fails_closed(client, repos, user, caplog):
    ok = _env(user, minutes=1)
    big = _env(user, children=[], name="huge")
    gone = uuid4()
    repos["environment"].list_items_by_ids.return_value = ([ok], [big])
    with caplog.at_level(logging.ERROR, logger="app.presentation.routes.environments"):
        html = client.get(f"/environments/reconcile?r={_token(ok.id)}&r={_token(big.id)}"
                          f"&r={_token(gone)}").text
    rows = _oob_rows(html)
    assert rows[f"environment-{ok.id}"] == "true"           # the rest is reconciled normally
    assert rows[f"environment-{gone}"] == "delete"
    big_tr = html[html.index(f'id="environment-{big.id}"'):]
    big_tr = big_tr[:big_tr.index("</tr>")]
    assert "could not be refreshed" in big_tr and "reload the page" in big_tr
    assert f'data-key="{list_key(big)}"' in big_tr
    assert "data-live" not in big_tr and "sse-swap" not in big_tr   # leaves the rotation
    assert str(big.id) in caplog.text and "child limit" in caplog.text
    assert repos["environment"].list_items_by_ids.await_count == 1   # nothing more was read


def test_environment_forged_and_unknown_ids_get_identical_directives(client, repos):
    forged, unknown = uuid4(), uuid4()
    html = client.get(f"/environments/reconcile?r={_token(forged)}&r={_token(unknown)}").text
    for row_id in (forged, unknown):
        assert f'<tr id="environment-{row_id}" hx-swap-oob="delete"></tr>' in html


# ── 5.4: access log ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("line, suppressed", [
    ('127.0.0.1:1 - "GET /book/vm/reconcile?filter=mine&r=x HTTP/1.1" 200', True),
    ('127.0.0.1:1 - "GET /environments/reconcile HTTP/1.1" 200', True),
    ('127.0.0.1:1 - "GET /bookings/abc/row HTTP/1.1" 200', True),
    ('127.0.0.1:1 - "GET /book/vm/list?filter=all HTTP/1.1" 200', False),
    ('127.0.0.1:1 - "GET /book/vm/rows?cursor=x HTTP/1.1" 200', False),
])
def test_access_log_filter(line, suppressed):
    from app.main import _SuppressRowPolling
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, line, None, None)
    assert _SuppressRowPolling().filter(record) is not suppressed
