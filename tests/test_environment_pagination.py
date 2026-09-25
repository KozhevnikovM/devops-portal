"""Unit tests for keyset pagination of the browser environments page (#467).

Covers the page-size setting, the opaque cursor codec, the first page / "Load more" routes and the
Load more control. Cursor boundaries and filter combinations against real SQL live in
tests/integration/test_environment_list_pagination.py.
"""
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.domain.entities import Environment, User
from app.domain.pagination import EnvironmentPage, KeysetCursor
from app.presentation.pagination import InvalidCursorError, decode_cursor, encode_cursor

_CURSOR = KeysetCursor(
    created_at=datetime(2026, 9, 25, 12, 30, 45, 123456, tzinfo=timezone.utc), id=uuid4(),
)


def _user():
    return User(id=uuid4(), username="me", password_hash="", role="user",
                is_active=True, created_at=datetime.now(timezone.utc))


def _env(user, name="dev"):
    now = datetime.now(timezone.utc)
    return Environment(
        id=uuid4(), name=name, blueprint_name="dev", user_id=str(user.id),
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        bookings=[], created_by=None, owner_username=user.username,
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
def repos():
    with (
        patch("app.presentation.routes.environments._env_repo") as env_repo,
        patch("app.presentation.routes.environments._blueprint_repo") as bp,
        patch("app.presentation.routes.environments._namespace_repo") as ns,
    ):
        env_repo.list_page = AsyncMock(return_value=EnvironmentPage())
        bp.list_active = AsyncMock(return_value=[])
        ns.list_available = AsyncMock(return_value=[])
        ns.list_held_standalone_by_user = AsyncMock(return_value=[])
        yield env_repo


def _load_more_query(html: str) -> dict[str, list[str]]:
    """Query params of the Load more button's hx-get URL (HTML-unescaped)."""
    start = html.index('hx-get="/environments/rows?') + len('hx-get="')
    url = html[start:html.index('"', start)].replace("&amp;", "&")
    return parse_qs(urlparse(url).query)


# ── Setting ──────────────────────────────────────────────────────────────────

def test_page_size_defaults_to_50():
    assert Settings().ENVIRONMENTS_PAGE_SIZE == 50


@pytest.mark.parametrize("bad", [0, -1])
def test_page_size_must_be_positive(bad):
    with pytest.raises(ValidationError):
        Settings(ENVIRONMENTS_PAGE_SIZE=bad)


# ── Cursor codec ─────────────────────────────────────────────────────────────

def test_cursor_round_trip_keeps_microseconds_and_offset():
    offset = KeysetCursor(
        created_at=datetime(2026, 1, 2, 3, 4, 5, 7, tzinfo=timezone(timedelta(hours=3))),
        id=uuid4(),
    )
    for cursor in (_CURSOR, offset):
        decoded = decode_cursor(encode_cursor(cursor))
        assert decoded == cursor
        assert decoded.created_at.microsecond == cursor.created_at.microsecond
        assert decoded.created_at.utcoffset() == cursor.created_at.utcoffset()


def test_cursor_is_url_safe_and_unpadded():
    token = encode_cursor(_CURSOR)
    assert "=" not in token and "+" not in token and "/" not in token


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize("token", [
    None,
    "",
    "!!!not-base64!!!",
    _b64("no-separator-here"),
    _b64(f"2026-09-25T12:00:00|{uuid4()}"),          # naive timestamp
    _b64("not-a-date|" + str(uuid4())),
    _b64("2026-09-25T12:00:00+00:00|not-a-uuid"),
    _b64(f"2026-09-25T12:00:00+00:00|{uuid4()}|extra"),
    base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode(),  # not UTF-8
])
def test_malformed_cursor_is_rejected(token):
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def _non_canonical(token: str) -> str:
    """Same bytes, different spelling: flip the unused low bits of the last character."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return token[:-1] + alphabet[alphabet.index(token[-1]) ^ 1]


_VALID = encode_cursor(_CURSOR)


@pytest.mark.parametrize("token", [
    pytest.param(_VALID + "!!!", id="trailing-junk"),
    pytest.param(_VALID[:10] + "!@#" + _VALID[10:], id="embedded-junk"),
    pytest.param(_VALID[:10] + " " + _VALID[10:], id="embedded-space"),
    pytest.param(_VALID + "\n", id="trailing-newline"),
    pytest.param(_VALID + "==", id="padding"),
    pytest.param(_VALID[:5] + "+" + _VALID[6:], id="standard-alphabet-plus"),
    pytest.param(_VALID[:5] + "/" + _VALID[6:], id="standard-alphabet-slash"),
    pytest.param(_VALID + "A", id="impossible-length"),
])
def test_valid_token_with_junk_is_rejected(token):
    """Regression (#475 review): a lenient decoder dropped junk and accepted these."""
    with pytest.raises(InvalidCursorError):
        decode_cursor(token)


def test_non_canonical_spelling_is_rejected():
    # No microseconds → a 62-byte payload, so the last character carries 2 unused bits.
    cursor = KeysetCursor(created_at=datetime(2026, 9, 25, 12, 30, 45, tzinfo=timezone.utc), id=uuid4())
    token = encode_cursor(cursor)
    assert len(token) % 4 != 0, "needs spare low bits in the last character"
    variant = _non_canonical(token)
    # The same bytes: a lenient decoder would accept it as this very cursor.
    padded = variant + "=" * (-len(variant) % 4)
    assert base64.urlsafe_b64decode(padded) == base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert decode_cursor(token) == cursor
    with pytest.raises(InvalidCursorError):
        decode_cursor(variant)


def test_rows_rejects_valid_token_with_trailing_junk(client, repos):
    resp = client.get(f"/environments/rows?cursor={_VALID}!!!")
    assert resp.status_code == 400
    repos.list_page.assert_not_called()


# ── GET /environments (first page) ───────────────────────────────────────────

def test_page_requests_first_page_with_configured_limit(client, repos, user):
    resp = client.get("/environments")
    assert resp.status_code == 200
    kwargs = repos.list_page.await_args.kwargs
    assert kwargs["after"] is None
    assert kwargs["limit"] == settings.ENVIRONMENTS_PAGE_SIZE
    assert kwargs["user_id"] == str(user.id)


def test_page_ignores_cursor_param(client, repos):
    """A bookmarked/pushed URL always opens at the top."""
    resp = client.get(f"/environments?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert repos.list_page.await_args.kwargs["after"] is None


def test_no_load_more_on_single_page(client, repos, user):
    repos.list_page.return_value = EnvironmentPage(items=[_env(user)], next_cursor=None)
    resp = client.get("/environments")
    assert resp.status_code == 200
    assert "environments-load-more" not in resp.text
    assert "Load more" not in resp.text


def test_load_more_shown_when_next_page_exists(client, repos, user):
    repos.list_page.return_value = EnvironmentPage(items=[_env(user)], next_cursor=_CURSOR)
    resp = client.get("/environments")
    assert 'id="environments-load-more"' in resp.text
    assert 'hx-target="closest tr"' in resp.text
    assert 'hx-swap="outerHTML"' in resp.text
    query = _load_more_query(resp.text)
    assert decode_cursor(query["cursor"][0]) == _CURSOR
    assert query["filter"] == ["mine"]
    assert "show_released" not in query and "label" not in query
    # The control is the last row of the tbody, after the environment rows.
    assert resp.text.index("environments-load-more") > resp.text.index(f"environment-{repos.list_page.return_value.items[0].id}")


def test_load_more_url_carries_filters(client, repos, user):
    repos.list_page.return_value = EnvironmentPage(items=[_env(user)], next_cursor=_CURSOR)
    resp = client.get("/environments?filter=all&show_released=1&label=dev%20stack")
    query = _load_more_query(resp.text)
    assert query["filter"] == ["all"]
    assert query["show_released"] == ["1"]
    assert query["label"] == ["dev stack"]


# ── GET /environments/rows (Load more) ───────────────────────────────────────

@pytest.mark.parametrize("query", ["", "?cursor=", "?cursor=garbage!!", f"?cursor={_b64('x|y')}"])
def test_rows_rejects_missing_or_malformed_cursor(client, repos, query):
    resp = client.get(f"/environments/rows{query}")
    assert resp.status_code == 400
    repos.list_page.assert_not_called()


def test_rows_forwards_cursor_and_filters(client, repos, user):
    resp = client.get(
        f"/environments/rows?cursor={encode_cursor(_CURSOR)}&filter=all&show_released=1&label=dev"
    )
    assert resp.status_code == 200
    kwargs = repos.list_page.await_args.kwargs
    assert kwargs["after"] == _CURSOR
    assert kwargs["user_id"] is None
    assert kwargs["include_released"] is True
    assert kwargs["label"] == "dev"
    assert kwargs["limit"] == settings.ENVIRONMENTS_PAGE_SIZE


def test_rows_mine_is_default_and_hides_released(client, repos, user):
    client.get(f"/environments/rows?cursor={encode_cursor(_CURSOR)}")
    kwargs = repos.list_page.await_args.kwargs
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["include_released"] is False
    assert kwargs["label"] is None


def test_rows_fragment_has_rows_and_next_control_but_no_page_chrome(client, repos, user):
    envs = [_env(user, "a"), _env(user, "b")]
    nxt = KeysetCursor(created_at=_CURSOR.created_at - timedelta(days=1), id=uuid4())
    repos.list_page.return_value = EnvironmentPage(items=envs, next_cursor=nxt)
    resp = client.get(f"/environments/rows?cursor={encode_cursor(_CURSOR)}&label=dev")
    assert resp.status_code == 200
    for e in envs:
        assert f'id="environment-{e.id}"' in resp.text
    assert "<html" not in resp.text and "Order an Environment" not in resp.text
    assert "<tbody" not in resp.text
    query = _load_more_query(resp.text)
    assert decode_cursor(query["cursor"][0]) == nxt
    assert query["label"] == ["dev"]


def test_rows_last_page_has_no_control(client, repos, user):
    repos.list_page.return_value = EnvironmentPage(items=[_env(user)], next_cursor=None)
    resp = client.get(f"/environments/rows?cursor={encode_cursor(_CURSOR)}")
    assert resp.status_code == 200
    assert "environments-load-more" not in resp.text


def test_rows_requires_authentication():
    """Refused the same way as the page itself: redirect to login (HTML) / 401 (JSON)."""
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    try:
        with patch("app.infrastructure.auth.get_current_user", AsyncMock(return_value=None)):
            cl = TestClient(app)
            url = f"/environments/rows?cursor={encode_cursor(_CURSOR)}"
            page = cl.get("/environments", follow_redirects=False)
            rows = cl.get(url, follow_redirects=False)
            rows_json = cl.get(url, headers={"Accept": "application/json"})
    finally:
        app.dependency_overrides.clear()
    assert rows.status_code == page.status_code == 302
    assert rows.headers["location"] == page.headers["location"] == "/auth/login"
    assert rows_json.status_code == 401


def test_rows_route_absent_from_schema():
    from app.main import app
    assert "/environments/rows" not in TestClient(app).get("/openapi.json").json()["paths"]
