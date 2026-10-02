"""Row versions, list keys and the live-row markup (#497 D4, D5).

A row version must change whenever anything the row displays changes — and only from list-safe
values — and every render path must emit the same version and list key for the same state, or
page reconciliation would either miss changes or re-render unchanged rows forever.
"""
import re
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.booking_list import BookingListItem, EnvironmentChildItem
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import EnvironmentPage, KeysetCursor, KeysetPage
from app.presentation.pagination import decode_cursor
from app.presentation.reconcile import environment_row_version, list_key, live_class, row_version

_NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
S = BookingStatus


def _user(role="user"):
    return User(id=uuid4(), username="me", password_hash="", role=role, is_active=True, created_at=_NOW)


def _booking(owner: User, status=S.PROVISIONING, **kw) -> Booking:
    base = dict(
        id=uuid4(), user_id=str(owner.id), status=status, ttl_minutes=240,
        expires_at=_NOW + timedelta(minutes=240), created_at=_NOW, resource_type=ResourceType.VM,
        image_name="ubuntu", hw_config_name="small", vm_ip="10.0.0.1", owner_username=owner.username,
        vm_password="pw", provisioning_log="line\n", config_roles=[{"name": "nginx", "vars": {}}],
    )
    base.update(kw)
    return Booking(**base)


def _as_list_item(b: Booking) -> BookingListItem:
    return BookingListItem(**{f.name: getattr(b, f.name) for f in fields(BookingListItem)})


def _list_item(**kw) -> BookingListItem:
    owner = _user()
    return replace(_as_list_item(_booking(owner)), **kw)


def _child(status=S.READY, **kw) -> EnvironmentChildItem:
    base = dict(id=uuid4(), status=status, resource_type=ResourceType.NAMESPACE, created_at=_NOW,
                environment_label="ns", namespace_name="ns-1", static_vm_name=None,
                static_vm_host=None, image_name=None, vm_ip=None, config_failed=False)
    base.update(kw)
    return EnvironmentChildItem(**base)


def _env(owner: User, children=None, **kw) -> Environment:
    from app.presentation.routes.environments import _annotate
    base = dict(id=uuid4(), name="dev", blueprint_name="dev", user_id=str(owner.id), ttl_minutes=240,
                expires_at=_NOW + timedelta(minutes=240), created_at=_NOW,
                bookings=children if children is not None else [_child()],
                owner_username=owner.username)
    base.update(kw)
    return _annotate(Environment(**base))


# ── 4.1: what changes a version ───────────────────────────────────────────────
_BOOKING_CHANGES = {
    "id": uuid4(), "user_id": "someone", "status": S.RELEASING, "resource_type": ResourceType.STATIC_VM,
    "ttl_minutes": 60, "expires_at": _NOW + timedelta(days=9), "created_at": _NOW - timedelta(days=1),
    "label": "new label", "status_message": "step 3/5", "config_failed": True,
    "environment_id": uuid4(), "owner_username": "other", "created_by": "disp",
    "created_by_username": "dispatcher", "image_id": uuid4(), "image_name": "debian",
    "hw_config_id": uuid4(), "hw_config_name": "large", "vm_ip": "10.0.0.9",
    "namespace_name": "ns-9", "cluster_name": "c9", "api_url": "https://k8s", "static_vm_name": "svm",
    "static_vm_host": "h9", "static_vm_username": "root", "has_provisioning_log": False,
    "config_role_names": ("docker",), "has_credentials": False, "queue_position": 3,
}


def test_every_booking_field_is_covered():
    assert set(_BOOKING_CHANGES) == {f.name for f in fields(BookingListItem)}


@pytest.mark.parametrize("name", sorted(_BOOKING_CHANGES))
def test_booking_version_changes_with_each_displayed_field(name):
    item = _list_item()
    assert row_version(replace(item, **{name: _BOOKING_CHANGES[name]})) != row_version(item)


def test_booking_version_is_a_16_hex_digest():
    assert re.fullmatch(r"[0-9a-f]{16}", row_version(_list_item()))


def test_booking_and_list_item_of_the_same_state_share_a_version():
    b = _booking(_user(), status=S.QUEUED, queue_position=2)
    assert row_version(b) == row_version(_as_list_item(b))


def test_booking_version_ignores_secret_values():
    owner = _user()
    b = _booking(owner)
    # Same displayed state (still has credentials / a log), different secret or log contents.
    assert row_version(replace(b, vm_password="other")) == row_version(b)
    assert row_version(replace(b, provisioning_log="another log\n")) == row_version(b)
    assert row_version(replace(b, startup_script="echo hi", extra_vars={"a": 1})) == row_version(b)


_ENV_CHANGES = {
    "name": "renamed", "blueprint_name": "other-bp", "owner_username": "other",
    "created_by": "disp", "created_by_username": "dispatcher", "ttl_minutes": 0,
    "expires_at": _NOW + timedelta(days=3),
}
_CHILD_CHANGES = {
    "id": uuid4(), "status": S.RELEASING, "environment_label": "web", "resource_type": ResourceType.VM,
    "namespace_name": "ns-2", "static_vm_name": "svm", "static_vm_host": "h", "image_name": "debian",
    "vm_ip": "10.1.1.1", "config_failed": True,
}


@pytest.mark.parametrize("name", sorted(_ENV_CHANGES))
def test_environment_version_changes_with_each_environment_field(name):
    owner = _user()
    env = _env(owner)
    changed = _env(owner, **{"id": env.id, "bookings": env.bookings, name: _ENV_CHANGES[name]})
    assert environment_row_version(changed) != environment_row_version(env)


@pytest.mark.parametrize("name", sorted(_CHILD_CHANGES))
def test_environment_version_changes_with_each_child_field(name):
    owner = _user()
    child = _child()
    env = _env(owner, children=[child])
    changed = _env(owner, children=[replace(child, **{name: _CHILD_CHANGES[name]})], id=env.id)
    assert environment_row_version(changed) != environment_row_version(env)


def test_environment_version_changes_with_derived_status_and_child_count():
    owner = _user()
    env = _env(owner, children=[_child(S.READY)])
    assert environment_row_version(_env(owner, children=[_child(S.READY), _child(S.READY)], id=env.id)) \
        != environment_row_version(env)


def test_environment_version_ignores_child_secrets_and_log():
    owner = _user()
    child = _booking(owner, status=S.READY, environment_label="web")
    env = _env(owner, children=[child])
    other = _env(owner, children=[replace(child, vm_password="x", provisioning_log="other\n",
                                          startup_script="echo")], id=env.id)
    assert environment_row_version(other) == environment_row_version(env)


def test_list_key_is_the_load_more_cursor():
    item = _list_item()
    assert decode_cursor(list_key(item)) == KeysetCursor(created_at=item.created_at, id=item.id)


@pytest.mark.parametrize("status, expected", [
    (S.QUEUED, "inflight"), (S.PENDING, "inflight"), (S.PROVISIONING, "inflight"),
    (S.CONFIGURING, "inflight"), (S.RETRY, "inflight"), (S.RELEASING, "inflight"),
    (S.READY, "settled"), (S.FAILED, "settled"), (S.RELEASED, None),
])
def test_live_class(status, expected):
    assert live_class(status) == expected
    assert live_class(status.value) == expected


# ── 4.4: row markup per status ────────────────────────────────────────────────
def _tr(html: str, row_id: str) -> str:
    start = html.index(f'id="{row_id}"')
    return html[html.rindex("<tr", 0, start):html.index(">", start)]


@pytest.mark.parametrize("status, live", [
    (S.PROVISIONING, "inflight"), (S.READY, "settled"), (S.FAILED, "settled"), (S.RELEASED, None),
])
def test_booking_row_markup(status, live):
    from app.presentation.templating import templates
    owner = _user()
    b = _booking(owner, status=status)
    tr = _tr(templates.get_template("partials/booking_row.html").render(booking=b, current_user=owner),
             f"booking-{b.id}")
    assert "hx-trigger" not in tr and "hx-get" not in tr
    assert f'data-key="{list_key(b)}"' in tr
    if live:
        assert f'sse-swap="booking-{b.id}"' in tr
        assert f'data-row-version="{row_version(b)}"' in tr
        assert f'data-live="{live}"' in tr
    else:
        assert "sse-swap" not in tr and "data-row-version" not in tr and "data-live" not in tr


@pytest.mark.parametrize("children, live", [
    ([S.PROVISIONING], "inflight"), ([S.READY], "settled"), ([S.FAILED], "settled"),
    ([S.RELEASED], None),
])
def test_environment_row_markup(children, live):
    from app.presentation.templating import templates
    owner = _user()
    env = _env(owner, children=[_child(s) for s in children])
    tr = _tr(templates.get_template("partials/environment_row.html").render(
        environment=env, current_user=owner), f"environment-{env.id}")
    assert "hx-trigger" not in tr and "hx-get" not in tr
    assert f'data-key="{list_key(env)}"' in tr
    if live:
        assert f'sse-swap="environment-{env.id}"' in tr
        assert f'data-row-version="{environment_row_version(env)}"' in tr
        assert f'data-live="{live}"' in tr
    else:
        assert "sse-swap" not in tr and "data-row-version" not in tr and "data-live" not in tr


# ── 4.2 / 4.3: every render path emits the same version and key ───────────────
def _markers(html: str, row_id: str) -> tuple:
    tr = _tr(html, row_id)
    return tuple(re.search(rf'{a}="([^"]*)"', tr).group(1) for a in ("data-row-version", "data-key"))


@pytest.fixture
def client():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    owner = _user()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: owner
    yield TestClient(app), owner
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_booking_render_paths_agree(client):
    from app.presentation.pagination import encode_cursor
    from app.presentation.routes import events
    cl, owner = client
    b = _booking(owner, status=S.QUEUED)
    item = _as_list_item(b)
    cursor = encode_cursor(KeysetCursor(created_at=_NOW + timedelta(days=1), id=uuid4()))
    with patch("app.presentation.routes.bookings._repo") as repo, \
         patch("app.presentation.routes.bookings._image_repo") as img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as svm, \
         patch.object(events, "_booking_repo") as sse_repo:
        img.list_active = AsyncMock(return_value=[])
        hw.list_active = AsyncMock(return_value=[])
        ns.list_available = AsyncMock(return_value=[])
        svm.list_available = AsyncMock(return_value=[])
        repo.list_page = AsyncMock(return_value=KeysetPage(items=[item]))
        repo.get = AsyncMock(return_value=b)
        positions = AsyncMock(side_effect=lambda session, rows: {r.id: 4 for r in rows})
        repo.queue_positions = positions
        sse_repo.get = AsyncMock(return_value=b)
        sse_repo.queue_positions = positions
        row_id = f"booking-{b.id}"
        rendered = {
            "page": cl.get("/book/vm").text,
            "list": cl.get("/book/vm/list").text,
            "rows": cl.get(f"/book/vm/rows?cursor={cursor}").text,
            "row": cl.get(f"/bookings/{b.id}/row").text,
            "sse": await events._render_booking_event(owner, str(b.id)),
        }
    versions = {name: _markers(html, row_id) for name, html in rendered.items()}
    assert len(set(versions.values())) == 1, versions
    assert versions["row"][0] == row_version(replace(b, queue_position=4))


@pytest.mark.asyncio
async def test_environment_render_paths_agree(client):
    from app.presentation.pagination import encode_cursor
    from app.presentation.routes import events
    cl, owner = client
    child = _booking(owner, status=S.PROVISIONING, environment_label="web")
    env = _env(owner, children=[child])
    cursor = encode_cursor(KeysetCursor(created_at=_NOW + timedelta(days=1), id=uuid4()))
    with patch("app.presentation.routes.environments._env_repo") as repo, \
         patch("app.presentation.routes.environments._blueprint_repo") as bp, \
         patch("app.presentation.routes.environments._namespace_repo") as ns, \
         patch("app.presentation.routes.environments._order_use_case") as order, \
         patch("app.presentation.routes.environments._update_name_use_case") as rename, \
         patch.object(events, "_env_repo") as sse_repo:
        bp.list_active = AsyncMock(return_value=[])
        ns.list_available = AsyncMock(return_value=[])
        ns.list_held_standalone_by_user = AsyncMock(return_value=[])
        repo.list_page = AsyncMock(return_value=EnvironmentPage(items=[env]))
        repo.get = AsyncMock(return_value=env)
        order.execute = AsyncMock(return_value=env)
        rename.execute = AsyncMock(return_value=env)
        sse_repo.get = AsyncMock(return_value=env)
        row_id = f"environment-{env.id}"
        rendered = {
            "page": cl.get("/environments").text,
            "list": cl.get("/environments/list").text,
            "rows": cl.get(f"/environments/rows?cursor={cursor}").text,
            "row": cl.get(f"/environments/{env.id}/row").text,
            "order": cl.post("/environments", data={"blueprint_name": "dev", "ttl_minutes": "240"}).text,
            "rename": cl.patch(f"/environments/{env.id}/name", data={"name": env.name}).text,
            "sse": await events._render_environment_event(owner, str(env.id)),
        }
    versions = {name: _markers(html, row_id) for name, html in rendered.items()}
    assert len(set(versions.values())) == 1, versions
