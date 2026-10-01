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
from uuid import UUID, uuid4

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
    BOOKING_PAGE_INDEXES,
    BOOKING_QUEUED,
    BOOKING_QUEUED_SQL,
    booking_page_key,
    booking_page_key_value,
)
from app.infrastructure.repositories._ordered_walk import (
    _PIN_ORDERED_WALK,
    _UNPIN_ORDERED_WALK,
)
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _label_page_keys_stmt,
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
        repo.queue_positions = AsyncMock(side_effect=lambda session, bookings: {b.id: 1 for b in bookings})
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


def test_label_scan_size_defaults_to_200():
    assert Settings().BOOKINGS_LABEL_SCAN_SIZE == 200


@pytest.mark.parametrize("page_size,scan_size", [(50, 50), (50, 10), (300, 200)])
def test_label_scan_size_must_exceed_the_page_size(page_size, scan_size):
    """#485: with scan size ≤ page size even a label matching everything would never fill a page."""
    with pytest.raises(ValidationError, match="BOOKINGS_LABEL_SCAN_SIZE"):
        Settings(BOOKINGS_PAGE_SIZE=page_size, BOOKINGS_LABEL_SCAN_SIZE=scan_size)


def test_label_scan_size_just_above_the_page_size_is_accepted():
    assert Settings(BOOKINGS_PAGE_SIZE=50, BOOKINGS_LABEL_SCAN_SIZE=51).BOOKINGS_LABEL_SCAN_SIZE == 51


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


def test_model_and_migration_0035_declare_the_same_index_names():
    """Names and predicates here; tests/integration/test_booking_list_pagination.py checks that the
    model's and the migration's definitions are identical as PostgreSQL stores them."""
    module = _migration_0035()
    migrated = {name: where for name, _, where in module._PAGE_INDEXES}
    for index in BOOKING_PAGE_INDEXES.values():
        where = index.dialect_options["postgresql"]["where"]
        assert migrated.pop(index.name) == (str(where) if where is not None else None), index.name
    assert migrated == {}


@pytest.mark.parametrize("scope,unreleased,owner,expected", [
    ("type", False, None, "t:VM"), ("type", True, None, "tl:VM"),
    ("owner", False, "u1", "o:u1:VM"), ("owner", True, "u1", "ol:u1:VM"),
    ("creator", False, "u1", "c:u1:VM"), ("creator", True, "u1", "cl:u1:VM"),
])
def test_page_key_values_are_distinct_per_branch_index(scope, unreleased, owner, expected):
    assert booking_page_key_value(scope, unreleased, "VM", owner) == expected
    sql = _sql(booking_page_key(scope, unreleased))
    assert sql.startswith(f"'{expected.split(':')[0]}:' || ")
    assert "%(" not in str(booking_page_key(scope, unreleased).compile(dialect=postgresql.dialect()))


# ── Page key statement ───────────────────────────────────────────────────────

def _keys_sql(user_id, resource_types, *, include_released=False, after=None) -> str:
    stmt = _page_keys_stmt(
        user_id, resource_types=resource_types, include_released=include_released,
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
    # Each branch constrains exactly its own page key (released hidden: the 'l' keys).
    tags = ("'ol:' || bookings.user_id", "'cl:' || bookings.created_by") if user_id else ("'tl:' || bookings.resource_type",)
    for tag in tags:
        assert sql.count(tag) == branches // len(tags), tag
    assert "bookings.user_id =" not in sql and "bookings.created_by =" not in sql
    assert "bookings.resource_type =" not in sql
    assert sql.count("bookings.created_by IS NOT NULL") == (branches // 2 if user_id else 0)


def test_page_keys_select_only_the_key_columns():
    stmt = _page_keys_stmt(
        "u1", resource_types=_VM_TYPES, include_released=False, limit=50, after=None,
    )
    assert [c.name for c in stmt.selected_columns] == ["created_at", "id"]


def test_released_filter_and_cursor_apply_to_every_branch():
    sql = _keys_sql("u1", _VM_TYPES, after=_CURSOR)
    assert sql.count("bookings.status != 'RELEASED'") == 4
    assert sql.count("(bookings.created_at, bookings.id) <") == 4
    assert "'RELEASED'" not in _keys_sql("u1", _VM_TYPES, include_released=True)


@pytest.mark.parametrize("types", [["NAMESPACE"], ["VM", "STATIC_VM"]])
def test_queue_rank_uses_the_literal_queued_predicate(types):
    """One rank() branch per resource type, each with the literal QUEUED predicate (#495)."""
    now = datetime.now(timezone.utc)
    stmt = _queue_rank_stmt({t: now for t in types}, [uuid4()])
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert sql.count("bookings.status = 'QUEUED'") == len(types)
    assert sql.count("rank() OVER (ORDER BY bookings.created_at)") == len(types)


_PREVIOUS = {"enable_bitmapscan": "on", "enable_seqscan": "off", "enable_sort": "on",
             "enable_indexscan": "off", "jit": "on"}


@pytest.mark.asyncio
async def test_key_query_runs_pinned_and_the_previous_settings_are_restored_before_phase_2():
    """Design Decision 10: pin → key query → restore (the exact previous values) → projection."""
    pin, keys, unpin, items = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    pin.one.return_value = SimpleNamespace(_mapping=_PREVIOUS)   # an operator's non-default values
    keys.all.return_value = [SimpleNamespace(created_at=_CURSOR.created_at, id=_CURSOR.id)]
    items.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[pin, keys, unpin, items])

    await BookingRepository().list_page(
        session, user_id="u1", resource_types=_VM_TYPES, label=None, include_released=False,
        limit=50, scan_size=200, after=None,
    )

    calls = session.execute.await_args_list
    assert calls[0].args[0] is _PIN_ORDERED_WALK
    assert "UNION ALL" in str(calls[1].args[0].compile(dialect=postgresql.dialect()))
    assert calls[2].args == (_UNPIN_ORDERED_WALK, _PREVIOUS)
    assert len(calls) == 4   # then phase 2, unpinned


def test_pin_leaves_only_the_sort_free_page_key_walk_and_is_transaction_local():
    """Review of #486: bitmap and seq scans off is not enough on its own — index scans are forced
    on too, or an operator's enable_indexscan = off would leave only a sequential scan. Review of
    #488: sorts are off too, so a full scan of another page index (which needs a sort to give page
    order) can't tie with the page-key walk when that index is empty or its statistics are stale.
    JIT is off because the penalised label-window sorts inflate the estimated cost past its
    threshold."""
    sql = str(_PIN_ORDERED_WALK)
    for name, value in (("enable_bitmapscan", "off"), ("enable_seqscan", "off"),
                        ("enable_sort", "off"), ("enable_indexscan", "on"), ("jit", "off")):
        assert f"current_setting('{name}')" in sql
        assert f"set_config('{name}', '{value}', true)" in sql
        assert f"set_config('{name}', :{name}, true)" in str(_UNPIN_ORDERED_WALK)


# ── Label scan window (#485) ─────────────────────────────────────────────────

def _label_sql(user_id="u1", types=_VM_TYPES, *, after=None) -> str:
    stmt = _label_page_keys_stmt(
        user_id, resource_types=types, label="db", include_released=False, limit=50,
        scan_size=200, after=after,
    )
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_label_window_is_materialized_and_the_walks_carry_no_label_test():
    """Decision 2: the label test runs only on the examined window, joined by primary key — pushed
    into the walks it would make them filtered walks again."""
    sql = _label_sql(after=_CURSOR)
    assert "page_window AS MATERIALIZED" in sql
    assert "page_examined AS MATERIALIZED" in sql
    window = sql[sql.index("page_window AS MATERIALIZED"):sql.index("page_examined AS MATERIALIZED")]
    assert "ILIKE" not in window.upper()
    assert window.count("FROM bookings") == 4   # the unlabelled page's four Mine branches
    assert window.count("(bookings.created_at, bookings.id) <") == 4
    assert window.count("LIMIT 201") == 5       # S + 1 per branch and for the merge: the probe
    assert sql.upper().count("ILIKE") == 1
    assert "JOIN bookings ON bookings.id = page_examined.id" in sql


def test_label_rows_end_with_an_explicit_sentinel_in_a_fixed_order():
    sql = _label_sql()
    assert "false AS is_window_end" in sql and "true AS is_window_end" in sql
    assert "FROM page_window) > 200" in sql     # the sentinel only when the probe exists
    assert sql.rstrip().endswith(
        "ORDER BY page_rows.is_window_end, page_rows.created_at DESC, page_rows.id DESC"
    )
    assert "LIMIT 51" in sql                    # at most limit + 1 matches


def _page_session(key_rows, *, projected=True):
    """A session for list_page: pin → key query (key_rows) → unpin → projection (if any keys kept)."""
    pin, keys, unpin, items = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    pin.one.return_value = SimpleNamespace(_mapping=_PREVIOUS)
    keys.all.return_value = key_rows
    items.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[pin, keys, unpin, items] if projected else [pin, keys, unpin])
    return session


def _key(i, *, end=False):
    return SimpleNamespace(created_at=_CURSOR.created_at - timedelta(seconds=i), id=uuid4(),
                           is_window_end=end)


async def _label_page(session, limit=3):
    return await BookingRepository().list_page(
        session, user_id="u1", resource_types=_VM_TYPES, label="db", include_released=False,
        limit=limit, scan_size=10, after=None,
    )


def _projected_ids(session) -> list:
    """The ids phase 2 projects: the one list-of-UUID parameter of the projection statement."""
    params = session.execute.await_args_list[3].args[0].compile(dialect=postgresql.dialect()).params
    [ids] = [v for v in params.values() if isinstance(v, list) and v and all(isinstance(i, UUID) for i in v)]
    return ids


@pytest.mark.asyncio
async def test_label_page_with_more_matches_than_the_limit_continues_after_the_last_shown():
    rows = [_key(1), _key(2), _key(3), _key(4), _key(9, end=True)]
    page = await _label_page(session := _page_session(rows))
    assert page.next_cursor == KeysetCursor(rows[2].created_at, rows[2].id)   # rule 1
    stmt = session.execute.await_args_list[1].args[0]
    assert "is_window_end" in [c.name for c in stmt.selected_columns]


@pytest.mark.asyncio
async def test_label_page_short_with_a_sentinel_continues_after_the_last_examined():
    rows = [_key(1), _key(9, end=True)]
    session = _page_session(rows)
    page = await _label_page(session)
    assert page.next_cursor == KeysetCursor(rows[1].created_at, rows[1].id)   # rule 2
    assert _projected_ids(session) == [rows[0].id]   # the sentinel is never a listed row


@pytest.mark.asyncio
async def test_label_page_with_no_matches_but_a_sentinel_is_empty_and_continues():
    end = _key(9, end=True)
    session = _page_session([end], projected=False)
    page = await _label_page(session)
    assert page.items == []
    assert page.next_cursor == KeysetCursor(end.created_at, end.id)
    assert session.execute.await_count == 3   # no projection for an empty page


@pytest.mark.asyncio
async def test_label_page_without_a_sentinel_is_the_last():
    session = _page_session([_key(1), _key(2)])
    page = await _label_page(session)
    assert page.next_cursor is None                                            # rule 3


@pytest.mark.asyncio
async def test_sentinel_sharing_a_matchs_key_is_listed_once():
    """The oldest examined booking matched: its key comes back twice, once per kind of row. The flag,
    not the key, decides — the booking is listed once and the scan continues after it."""
    match = _key(5)
    end = SimpleNamespace(created_at=match.created_at, id=match.id, is_window_end=True)
    session = _page_session([_key(1), match, end])
    page = await _label_page(session)
    ids = _projected_ids(session)
    assert ids.count(match.id) == 1 and len(ids) == 2
    assert page.next_cursor == KeysetCursor(match.created_at, match.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, "", "   "])
async def test_blank_label_takes_the_unlabelled_key_query(label):
    session = _page_session([_key(1)])
    await BookingRepository().list_page(
        session, user_id="u1", resource_types=_VM_TYPES, label=label, include_released=False,
        limit=3, scan_size=10, after=None,
    )
    stmt = session.execute.await_args_list[1].args[0]
    assert [c.name for c in stmt.selected_columns] == ["created_at", "id"]


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
    assert kwargs["scan_size"] == settings.BOOKINGS_LABEL_SCAN_SIZE
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["resource_types"] == types
    assert kwargs["include_released"] is False


def test_page_ignores_cursor_param(client, repo):
    """A bookmarked/pushed URL always opens at the top."""
    resp = client.get(f"/book/vm?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert repo.list_page.await_args.kwargs["after"] is None


# Every list surface: the page, the filter-change fragment (#494) and Load more (#495).
_LIST_URLS = ["/book/vm", "/book/vm/list", f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}"]


@pytest.mark.parametrize("url", _LIST_URLS)
def test_queue_positions_are_read_once_for_the_pages_queued_rows(client, repo, user, url):
    queued = [_booking(user, BookingStatus.QUEUED),
              _booking(user, BookingStatus.QUEUED, ResourceType.STATIC_VM)]
    items = [queued[0], _booking(user), queued[1], _booking(user, BookingStatus.FAILED)]
    repo.list_page.return_value = KeysetPage(items=items, next_cursor=_CURSOR)
    assert client.get(url).status_code == 200
    repo.queue_positions.assert_awaited_once()
    assert repo.queue_positions.await_args.args[1] == queued


@pytest.mark.parametrize("url", _LIST_URLS)
def test_no_queue_position_read_without_queued_rows(client, repo, user, url):
    items = [_booking(user), _booking(user, BookingStatus.FAILED), _booking(user, BookingStatus.RELEASED)]
    repo.list_page.return_value = KeysetPage(items=items, next_cursor=None)
    assert client.get(url).status_code == 200
    repo.queue_positions.assert_not_awaited()


def test_rank_renders_and_a_missing_rank_renders_a_dash(client, repo, user):
    """A row promoted before the rank read has no entry in the result (design D2)."""
    ranked, promoted = _booking(user, BookingStatus.QUEUED), _booking(user, BookingStatus.QUEUED)
    repo.list_page.return_value = KeysetPage(items=[ranked, promoted], next_cursor=None)
    repo.queue_positions = AsyncMock(return_value={ranked.id: 7})
    html = client.get("/book/vm").text
    assert "Queued — position 7" in html
    assert "Queued — position —" in html


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
    assert kwargs["scan_size"] == settings.BOOKINGS_LABEL_SCAN_SIZE


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


# ── Short label pages and the label-aware empty state (#485) ─────────────────

def _control_text(html: str) -> str:
    anchor = html.index('id="bookings-load-more"')
    start = html.index(">", html.index("<button", anchor)) + 1
    return html[start:html.index("</button>", start)].strip()


def _empty_text(html: str) -> str:
    anchor = html.index('id="empty-row"')
    start = html.index(">", html.index("<td", anchor)) + 1
    return " ".join(html[start:html.index("</td>", start)].split())


def test_full_page_with_a_next_page_says_load_more(client, repo, user):
    items = [_booking(user) for _ in range(settings.BOOKINGS_PAGE_SIZE)]
    repo.list_page.return_value = KeysetPage(items=items, next_cursor=_CURSOR)
    assert _control_text(client.get("/book/vm?label=db").text) == "Load more"


@pytest.mark.parametrize("path", ["/book/vm?label=db", f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}&label=db"])
def test_short_page_with_a_next_page_says_search_older_bookings(client, repo, user, path):
    repo.list_page.return_value = KeysetPage(items=[_booking(user)], next_cursor=_CURSOR)
    resp = client.get(path)
    assert _control_text(resp.text) == "Search older bookings"
    assert "Load more" not in resp.text


def test_rows_fragment_full_page_says_load_more(client, repo, user):
    items = [_booking(user) for _ in range(settings.BOOKINGS_PAGE_SIZE)]
    repo.list_page.return_value = KeysetPage(items=items, next_cursor=_CURSOR)
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}&label=db")
    assert _control_text(resp.text) == "Load more"


def test_empty_page_without_a_label_says_no_bookings_yet(client, repo):
    assert _empty_text(client.get("/book/namespace").text) == "No namespace bookings yet."


def test_empty_label_page_that_continues_says_among_the_most_recent(client, repo):
    repo.list_page.return_value = KeysetPage(items=[], next_cursor=_CURSOR)
    resp = client.get("/book/vm?label=db")
    assert _empty_text(resp.text) == "No bookings matching “db” among the most recent bookings."
    assert "bookings yet" not in resp.text
    assert _control_text(resp.text) == "Search older bookings"
    assert decode_cursor(_load_more_query(resp.text)["cursor"][0]) == _CURSOR
    # The control still follows the empty row, so the order form's prepend removes only that row.
    assert resp.text.index('id="bookings-load-more"') > resp.text.index('id="empty-row"')


def test_empty_label_page_that_ends_says_no_bookings_match(client, repo):
    repo.list_page.return_value = KeysetPage(items=[], next_cursor=None)
    resp = client.get("/book/vm?label=db")
    assert _empty_text(resp.text) == "No bookings match “db”."
    assert "bookings-load-more" not in resp.text


def test_blank_label_keeps_the_no_bookings_yet_message(client, repo):
    assert _empty_text(client.get("/book/vm?label=%20%20").text) == "No vm bookings yet."


def test_empty_rows_page_with_a_next_page_holds_only_the_new_control(client, repo):
    nxt = KeysetCursor(created_at=_CURSOR.created_at - timedelta(days=1), id=uuid4())
    repo.list_page.return_value = KeysetPage(items=[], next_cursor=nxt)
    resp = client.get(f"/book/vm/rows?cursor={encode_cursor(_CURSOR)}&label=db")
    assert resp.status_code == 200
    assert 'id="empty-row"' not in resp.text
    assert resp.text.count("<tr") == 1 and 'id="bookings-load-more"' in resp.text
    assert _control_text(resp.text) == "Search older bookings"
    assert decode_cursor(_load_more_query(resp.text)["cursor"][0]) == nxt
