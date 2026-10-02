"""Integration: page reconciliation's batch reads against real Postgres (#497 D3, D3a, D8).

- `BookingRepository.list_items_by_ids` returns exactly the requested ids visible on the page: the
  page's kinds, and for Mine the owner/creator rule. Show released and the label filter don't apply.
- `EnvironmentRepository.list_items_by_ids` does the same for environments and reads their children
  through the bounded child read — the same children `list_page` loads, as list-safe items — or
  reports an environment over the child limit without its children.
- `newest_key` is one data statement (plus the ordered-walk pin and restore) and matches the first
  page.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, insert

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import BookingModel, EnvironmentModel
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_bookings = BookingRepository()
_envs = EnvironmentRepository()
# Far in the future so seeded rows are the newest in the test database.
_BASE = datetime(2999, 6, 1, tzinfo=timezone.utc)


def _booking(owner, *, i=0, status=S.READY, rtype="VM", label=None, created_by=None, env_id=None):
    return {
        "id": uuid4(), "user_id": owner, "status": status.value, "resource_type": rtype,
        "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT,
        "created_at": _BASE - timedelta(seconds=i), "label": label, "created_by": created_by,
        "environment_id": env_id, "vm_password": "s3cret",
    }


async def _insert(session, model, rows):
    await session.execute(insert(model), rows)
    await session.flush()
    return [r["id"] for r in rows]


class _Statements:
    """Counts every statement the session's connection sends."""

    def __init__(self, session):
        self.count = 0
        self._engine = session.bind.sync_engine if hasattr(session.bind, "sync_engine") else session.bind

    def _on(self, *args):
        self.count += 1

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on)


# ── bookings ──────────────────────────────────────────────────────────────────
async def test_booking_batch_read_returns_the_visible_subset(async_session):
    me, other, dispatcher = (f"inttest-{uuid4()}" for _ in range(3))
    mine, theirs, dispatched, ns, released, labelled = await _insert(async_session, BookingModel, [
        _booking(me, i=0),
        _booking(other, i=1),
        _booking(other, i=2, created_by=me),
        _booking(me, i=3, rtype="NAMESPACE"),
        _booking(me, i=4, status=S.RELEASED),
        _booking(me, i=5, label="zzz"),
    ])
    ids = [mine, theirs, dispatched, ns, released, labelled, uuid4()]

    async def visible(user_id, types):
        items = await _bookings.list_items_by_ids(async_session, ids, user_id=user_id,
                                                  resource_types=types)
        return {b.id for b in items}

    # Mine: owned or dispatched; a wrong kind and an unknown id are simply absent.
    assert await visible(me, ["VM", "STATIC_VM"]) == {mine, dispatched, released, labelled}
    # All: every booking of the page's kinds.
    assert await visible(None, ["VM", "STATIC_VM"]) == {mine, theirs, dispatched, released, labelled}
    assert await visible(None, ["NAMESPACE"]) == {ns}


async def test_booking_batch_read_carries_no_credentials(async_session):
    me = f"inttest-{uuid4()}"
    [bid] = await _insert(async_session, BookingModel, [_booking(me)])
    [item] = await _bookings.list_items_by_ids(async_session, [bid], user_id=me, resource_types=["VM"])
    assert not hasattr(item, "vm_password")
    assert "s3cret" not in repr(item)


async def test_booking_newest_key_is_one_data_statement_and_matches_the_first_page(async_session):
    me = f"inttest-{uuid4()}"
    ids = await _insert(async_session, BookingModel, [_booking(me, i=i) for i in range(3)])
    with _Statements(async_session) as stmts:
        key = await _bookings.newest_key(async_session, user_id=me, resource_types=["VM", "STATIC_VM"],
                                         label=None, include_released=False, scan_size=200)
    assert key is not None and key.id == ids[0]
    assert stmts.count == 3   # pin + key walk + restore
    page = await _bookings.list_page(async_session, user_id=me, resource_types=["VM", "STATIC_VM"],
                                     label=None, include_released=False, limit=50, scan_size=200,
                                     after=None)
    assert page.items[0].id == key.id


async def test_booking_newest_key_honours_label_and_released_filters(async_session):
    me = f"inttest-{uuid4()}"
    rel, lab, plain = await _insert(async_session, BookingModel, [
        _booking(me, i=0, status=S.RELEASED, label="alpha"),
        _booking(me, i=1, label="alpha"),
        _booking(me, i=2),
    ])

    async def newest(**kw):
        key = await _bookings.newest_key(async_session, user_id=me, resource_types=["VM"],
                                         scan_size=200, **kw)
        return key and key.id

    assert await newest(label=None, include_released=True) == rel
    assert await newest(label=None, include_released=False) == lab
    assert await newest(label="alp", include_released=False) == lab
    assert await newest(label="nomatch", include_released=True) is None


# ── environments ──────────────────────────────────────────────────────────────
async def _env(session, owner, *, i=0, created_by=None, children=(S.READY,), name="env"):
    env_id = uuid4()
    await session.execute(insert(EnvironmentModel), [{
        "id": env_id, "name": name, "user_id": owner, "ttl_minutes": 60,
        "expires_at": PERMANENT_EXPIRES_AT, "construction_complete": True,
        "created_at": _BASE - timedelta(seconds=i), "created_by": created_by,
    }])
    if children:
        await _insert(session, BookingModel, [
            {**_booking(owner, i=i * 100 + k, status=s, rtype="NAMESPACE", env_id=env_id),
             "environment_label": f"c{k}"}
            for k, s in enumerate(children)
        ])
    await session.flush()
    return env_id


async def test_environment_batch_read_returns_the_visible_subset(async_session):
    me, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    mine = await _env(async_session, me, i=0)
    theirs = await _env(async_session, other, i=1)
    dispatched = await _env(async_session, other, i=2, created_by=me)
    released = await _env(async_session, me, i=3, children=(S.RELEASED,), name="zzz")
    ids = [mine, theirs, dispatched, released, uuid4()]

    async def visible(user_id):
        items, over = await _envs.list_items_by_ids(async_session, ids, user_id=user_id, child_limit=25)
        assert over == set()
        return {e.id for e in items}

    assert await visible(me) == {mine, dispatched, released}
    assert await visible(None) == {mine, theirs, dispatched, released}


async def test_environment_children_match_list_page(async_session):
    me = f"inttest-{uuid4()}"
    env_id = await _env(async_session, me, children=(S.READY, S.RELEASING, S.FAILED))
    [listed] = (await _envs.list_page(async_session, user_id=me, label=None, include_released=True,
                                      limit=1, after=None)).items
    [reconciled], over = await _envs.list_items_by_ids(async_session, [env_id], user_id=me,
                                                       child_limit=25)
    assert over == set()
    assert listed.id == reconciled.id == env_id
    fields = ("id", "status", "resource_type", "environment_label", "namespace_name",
              "static_vm_name", "static_vm_host", "image_name", "vm_ip", "config_failed")
    assert ([tuple(getattr(c, f) for f in fields) for c in listed.bookings]
            == [tuple(getattr(c, f) for f in fields) for c in reconciled.bookings])


async def test_environment_at_the_child_limit_is_complete(async_session):
    me = f"inttest-{uuid4()}"
    env_id = await _env(async_session, me, children=(S.READY,) * 4)
    [env], over = await _envs.list_items_by_ids(async_session, [env_id], user_id=me, child_limit=4)
    assert over == set() and len(env.bookings) == 4


async def test_environment_over_the_child_limit_is_reported_without_children(async_session):
    me = f"inttest-{uuid4()}"
    ok = await _env(async_session, me, i=0, children=(S.READY,) * 2)
    big = await _env(async_session, me, i=1, children=(S.READY,) * 9)
    with _Statements(async_session) as stmts:
        items, over = await _envs.list_items_by_ids(async_session, [ok, big], user_id=me, child_limit=4)
    assert over == {big}
    assert [e.id for e in items] == [ok] and len(items[0].bookings) == 2
    assert stmts.count == 2   # environments + bounded children; nothing more for the big one


async def test_environment_newest_key_is_one_data_statement(async_session):
    me = f"inttest-{uuid4()}"
    newest = await _env(async_session, me, i=0)
    await _env(async_session, me, i=1)
    with _Statements(async_session) as stmts:
        key = await _envs.newest_key(async_session, user_id=me, label=None, include_released=False)
    assert key.id == newest
    assert stmts.count == 3   # pin + key walk + restore
