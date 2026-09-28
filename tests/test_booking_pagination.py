"""Unit tests for keyset pagination of the browser bookings pages (#479).

Covers the page-size setting, the partial-index predicate spellings, the page key statement, the
first page / "Load more" routes and the Load more control. The cursor codec is shared with the
environments page and tested in tests/test_environment_pagination.py. Cursor boundaries, filter
combinations and the page-size read bound against real SQL live in
tests/integration/test_booking_list_pagination.py.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.config import Settings, settings
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import KeysetCursor, KeysetPage
from app.infrastructure.database.models import (
    BOOKING_NOT_RELEASED,
    BOOKING_NOT_RELEASED_SQL,
    BOOKING_QUEUED,
    BOOKING_QUEUED_SQL,
    BookingModel,
)
from app.infrastructure.repositories.booking_repo import (
    _PIN_ORDERED_WALK,
    _UNPIN_ORDERED_WALK,
    BookingRepository,
    _page_keys_stmt,
    _queue_rank_stmt,
)
from app.presentation.pagination import decode_cursor, encode_cursor

_CURSOR = KeysetCursor(
    created_at=datetime(2026, 9, 28, 12, 30, 45, 123456, tzinfo=timezone.utc), id=uuid4(),
)
_VM_TYPES = ["VM", "STATIC_VM"]


def _user():
    return User(id=uuid4(), username="me", password_hash="", role="user",
                is_active=True, created_at=datetime.now(timezone.utc))


def _booking(user, status=BookingStatus.READY, resource_type=ResourceType.VM):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=str(user.id), status=status, resource_type=resource_type,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        owner_username=user.username,
    )


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
def repo():
    with (
        patch("app.presentation.routes.bookings._repo") as repo,
        patch("app.presentation.routes.bookings._image_repo") as img,
        patch("app.presentation.routes.bookings._hw_config_repo") as hw,
        patch("app.presentation.routes.bookings._namespace_repo") as ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as svm,
        patch("app.presentation.routes.bookings._role_repo") as role,
    ):
        repo.list_page = AsyncMock(return_value=KeysetPage())
        repo.queue_position = AsyncMock(return_value=1)
        img.list_active = AsyncMock(return_value=[])
        hw.list_active = AsyncMock(return_value=[])
        ns.list_available = AsyncMock(return_value=[])
        svm.list_available = AsyncMock(return_value=[])
        role.list_active = AsyncMock(return_value=[])
        yield repo


def _load_more_url(html: str) -> str:
    """The Load more button's hx-get URL (HTML-unescaped)."""
    anchor = html.index('id="bookings-load-more"')
    start = html.index('hx-get="', anchor) + len('hx-get="')
    return html[start:html.index('"', start)].replace("&amp;", "&")


def _load_more_query(html: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(_load_more_url(html)).query)


# ── Setting ──────────────────────────────────────────────────────────────────

def test_page_size_defaults_to_50():
    assert Settings().BOOKINGS_PAGE_SIZE == 50


@pytest.mark.parametrize("bad", [0, -1])
def test_page_size_must_be_positive(bad):
    with pytest.raises(ValidationError):
        Settings(BOOKINGS_PAGE_SIZE=bad)


# ── Partial-index predicates ─────────────────────────────────────────────────

def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_query_predicates_are_the_index_predicates_as_literals():
    """The planner matches a partial index only against a literal predicate (#479)."""
    assert _sql(BOOKING_NOT_RELEASED) == "bookings.status != 'RELEASED'"
    assert BOOKING_NOT_RELEASED_SQL == "status <> 'RELEASED'"
    assert _sql(BOOKING_QUEUED) == "bookings.status = 'QUEUED'"
    assert BOOKING_QUEUED_SQL == "status = 'QUEUED'"
    # Neither is a bound parameter, even without literal_binds.
    assert "%(" not in str(BOOKING_NOT_RELEASED.compile(dialect=postgresql.dialect()))
    assert "%(" not in str(BOOKING_QUEUED.compile(dialect=postgresql.dialect()))


def _migration_0035():
    path = Path(__file__).parent.parent / "alembic" / "versions" / "0035_bookings_page_indexes.py"
    spec = importlib.util.spec_from_file_location("migration_0035", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_indexes_match_migration_0035():
    declared = {}
    for index in BookingModel.__table__.indexes:
        where = index.dialect_options["postgresql"]["where"]
        declared[index.name] = ([c.name for c in index.columns], str(where) if where is not None else None)
    for name, columns, where in _migration_0035()._INDEXES:
        assert declared[name] == (columns, where), name


# ── Page key statement ───────────────────────────────────────────────────────

def _keys_sql(user_id, resource_types, *, include_released=False, label=None, after=None) -> str:
    stmt = _page_keys_stmt(
        user_id, resource_types=resource_types, label=label, include_released=include_released,
        limit=50, after=after,
    )
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("user_id,types,branches", [
    (None, ["NAMESPACE"], 1), (None, _VM_TYPES, 2), ("u1", ["NAMESPACE"], 2), ("u1", _VM_TYPES, 4),
])
def test_one_limited_ordered_branch_per_owner_column_and_type(user_id, types, branches):
    sql = _keys_sql(user_id, types)
    assert sql.count("FROM bookings") == branches
    assert sql.count("ORDER BY bookings.created_at DESC, bookings.id DESC") == branches
    assert sql.count(" OR ") == 0 and " IN " not in sql
    assert sql.count("bookings.user_id =") == (branches // 2 if user_id else 0)
    assert sql.count("bookings.created_by =") == (branches // 2 if user_id else 0)


def test_page_keys_select_only_the_key_columns():
    stmt = _page_keys_stmt(
        "u1", resource_types=_VM_TYPES, label=None, include_released=False, limit=50, after=None,
    )
    assert [c.name for c in stmt.selected_columns] == ["created_at", "id"]


def test_released_filter_and_cursor_apply_to_every_branch():
    sql = _keys_sql("u1", _VM_TYPES, after=_CURSOR)
    assert sql.count("bookings.status != 'RELEASED'") == 4
    assert sql.count("(bookings.created_at, bookings.id) <") == 4
    assert "'RELEASED'" not in _keys_sql("u1", _VM_TYPES, include_released=True)


def test_queue_rank_uses_the_literal_queued_predicate():
    sql = str(_queue_rank_stmt("VM", datetime.now(timezone.utc)).compile(dialect=postgresql.dialect()))
    assert "bookings.status = 'QUEUED'" in sql


@pytest.mark.asyncio
async def test_key_query_runs_pinned_and_the_previous_settings_are_restored_before_phase_2():
    """Design Decision 10: pin → key query → restore (the exact previous values) → projection."""
    pin, keys, unpin, items = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    pin.one.return_value = SimpleNamespace(bitmapscan="on", seqscan="off")   # operator's value
    keys.all.return_value = [SimpleNamespace(created_at=_CURSOR.created_at, id=_CURSOR.id)]
    items.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[pin, keys, unpin, items])

    await BookingRepository().list_page(
        session, user_id="u1", resource_types=_VM_TYPES, label=None, include_released=False,
        limit=50, after=None,
    )

    calls = session.execute.await_args_list
    assert calls[0].args[0] is _PIN_ORDERED_WALK
    assert "UNION ALL" in str(calls[1].args[0].compile(dialect=postgresql.dialect()))
    assert calls[2].args == (_UNPIN_ORDERED_WALK, {"bitmapscan": "on", "seqscan": "off"})
    assert len(calls) == 4   # then phase 2, unpinned


def test_pin_is_transaction_local_and_switches_off_bitmap_and_seq_scans():
    sql = str(_PIN_ORDERED_WALK)
    assert "set_config('enable_bitmapscan', 'off', true)" in sql
    assert "set_config('enable_seqscan', 'off', true)" in sql
    assert "enable_sort" not in sql
    assert str(_UNPIN_ORDERED_WALK).count(", true)") == 2


# ── First page: GET / , /book/vm, /book/namespace ────────────────────────────

@pytest.mark.parametrize("path,types", [
    ("/", _VM_TYPES), ("/book/vm", _VM_TYPES), ("/book/namespace", ["NAMESPACE"]),
])
def test_page_requests_first_page_with_configured_limit(client, repo, user, path, types):
    resp = client.get(path)
    assert resp.status_code == 200
    kwargs = repo.list_page.await_args.kwargs
    assert kwargs["after"] is None
    assert kwargs["limit"] == settings.BOOKINGS_PAGE_SIZE
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["resource_types"] == types
    assert kwargs["include_released"] is False


def test_page_ignores_cursor_param(client, repo):
    """A bookmarked/pushed URL always opens at the top."""
    resp = client.get(f"/book/vm?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert repo.list_page.await_args.kwargs["after"] is None


def test_queue_positions_are_looked_up_only_for_the_pages_queued_rows(client, repo, user):
    items = [_booking(user, BookingStatus.QUEUED), _booking(user), _booking(user, BookingStatus.QUEUED)]
    repo.list_page.return_value = KeysetPage(items=items, next_cursor=_CURSOR)
    client.get("/book/vm")
    assert repo.queue_position.await_count == 2


def test_no_load_more_on_single_page(client, repo, user):
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=None)
    resp = client.get("/book/vm")
    assert resp.status_code == 200
    assert "bookings-load-more" not in resp.text
    assert "Load more" not in resp.text


def test_load_more_shown_as_last_row_when_next_page_exists(client, repo, user):
    booking = _booking(user)
    repo.list_page.return_value = KeysetPage(items=[booking], next_cursor=_CURSOR)
    resp = client.get("/book/vm")
    assert 'id="bookings-load-more"' in resp.text
    assert 'colspan="9"' in resp.text[resp.text.index('id="bookings-load-more"'):]
    assert 'hx-target="closest tr"' in resp.text
    assert 'hx-swap="outerHTML"' in resp.text
    assert _load_more_url(resp.text).startswith("/book/vm/rows?")
    query = _load_more_query(resp.text)
    assert decode_cursor(query["cursor"][0]) == _CURSOR
    assert query["filter"] == ["mine"]
    assert "show_released" not in query and "label" not in query
    # The control follows the booking rows and closes the tbody the order form prepends into.
    control = resp.text.index("bookings-load-more")
    assert control > resp.text.index(f'id="booking-{booking.id}"')
    assert "<tr" not in resp.text[control:resp.text.index("</tbody>", control)]


def test_root_page_loads_more_from_the_vm_rows_route(client, repo, user):
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=_CURSOR)
    assert _load_more_url(client.get("/").text).startswith("/book/vm/rows?")


def test_namespace_page_loads_more_from_its_own_rows_route(client, repo, user):
    repo.list_page.return_value = KeysetPage(
        items=[_booking(user, resource_type=ResourceType.NAMESPACE)], next_cursor=_CURSOR,
    )
    assert _load_more_url(client.get("/book/namespace").text).startswith("/book/namespace/rows?")


def test_load_more_url_carries_filters(client, repo, user):
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=_CURSOR)
    resp = client.get("/book/vm?filter=all&show_released=1&label=perf%20run")
    query = _load_more_query(resp.text)
    assert query["filter"] == ["all"]
    assert query["show_released"] == ["1"]
    assert query["label"] == ["perf run"]


# ── GET /book/{vm,namespace}/rows (Load more) ────────────────────────────────

_ROWS = ["/book/vm/rows", "/book/namespace/rows"]


@pytest.mark.parametrize("path", _ROWS)
@pytest.mark.parametrize("query", [
    "", "?cursor=", "?cursor=garbage!!", f"?cursor={encode_cursor(_CURSOR)}!!!",
])
def test_rows_rejects_missing_or_malformed_cursor(client, repo, path, query):
    resp = client.get(f"{path}{query}")
    assert resp.status_code == 400
    repo.list_page.assert_not_called()


@pytest.mark.parametrize("path,types", [("/book/vm/rows", _VM_TYPES), ("/book/namespace/rows", ["NAMESPACE"])])
def test_rows_forwards_cursor_filters_and_the_paths_types(client, repo, path, types):
    resp = client.get(f"{path}?cursor={encode_cursor(_CURSOR)}&filter=all&show_released=1&label=dev")
    assert resp.status_code == 200
    kwargs = repo.list_page.await_args.kwargs
    assert kwargs["after"] == _CURSOR
    assert kwargs["user_id"] is None
    assert kwargs["include_released"] is True
    assert kwargs["label"] == "dev"
    assert kwargs["resource_types"] == types
    assert kwargs["limit"] == settings.BOOKINGS_PAGE_SIZE


def test_rows_mine_is_default_and_hides_released(client, repo, user):
    client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}")
    kwargs = repo.list_page.await_args.kwargs
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["include_released"] is False
    assert kwargs["label"] is None


def test_rows_fragment_has_rows_and_next_control_but_no_page_chrome(client, repo, user):
    bookings = [_booking(user), _booking(user)]
    nxt = KeysetCursor(created_at=_CURSOR.created_at - timedelta(days=1), id=uuid4())
    repo.list_page.return_value = KeysetPage(items=bookings, next_cursor=nxt)
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}&label=dev")
    assert resp.status_code == 200
    for b in bookings:
        assert f'id="booking-{b.id}"' in resp.text
    assert "<html" not in resp.text and "<tbody" not in resp.text
    assert 'id="empty-row"' not in resp.text
    assert _load_more_url(resp.text).startswith("/book/vm/rows?")
    query = _load_more_query(resp.text)
    assert decode_cursor(query["cursor"][0]) == nxt
    assert query["label"] == ["dev"]


def test_rows_fragment_rows_never_take_the_first_row_menu_position(client, repo, user):
    """Appended rows are never the table's first row, so their action menu opens upward."""
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=None)
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}")
    assert "top-full mt-1 w-48" not in resp.text


def test_empty_rows_page_only_removes_the_control(client, repo):
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert resp.text.strip() == ""


def test_rows_last_page_has_no_control(client, repo, user):
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=None)
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert "bookings-load-more" not in resp.text


@pytest.mark.parametrize("path", _ROWS)
def test_rows_requires_authentication(path):
    """Refused the same way as the page itself: redirect to login (HTML) / 401 (JSON)."""
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    try:
        with patch("app.infrastructure.auth.get_current_user", AsyncMock(return_value=None)):
            cl = TestClient(app)
            url = f"{path}?cursor={encode_cursor(_CURSOR)}"
            page = cl.get("/book/vm", follow_redirects=False)
            rows = cl.get(url, follow_redirects=False)
            rows_json = cl.get(url, headers={"Accept": "application/json"})
    finally:
        app.dependency_overrides.clear()
    assert rows.status_code == page.status_code == 302
    assert rows.headers["location"] == page.headers["location"] == "/auth/login"
    assert rows_json.status_code == 401


def test_rows_routes_absent_from_schema():
    from app.main import app
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    for path in _ROWS:
        assert path not in paths
