"""Integration: what one page reconciliation request costs, against real Postgres (#497 D3, D3a).

For 1 id, `RECONCILE_MAX_IDS` ids, and every batch of a rotation over three loaded pages (150 rows),
the statement count is the same and within the stated maximum — 7 for a bookings page (batch read,
queue-rank pin/read/restore, newest-key pin/walk/restore), 5 for environments (batch read, bounded
children, newest-key pin/walk/restore) — counting every statement the session sends. No order-form
catalog is read. The environment child read stops at the effective child limit + 1 per
environment, even for one that breaks the limit through a direct database edit, which fails
closed. Response bytes for a fully changed and an unchanged batch are printed (run with ``-s``).
Authorization is the batch read: under Mine a row the viewer neither owns nor created comes back as
a removal; a request built for All is answered with All's visibility (the server is stateless).
"""
import re
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, insert

from app.config import settings
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import User
from app.infrastructure.auth import require_user
from app.infrastructure.database.models import BookingModel, EnvironmentModel, UserModel
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories import environment_repo
from app.main import app
from app.presentation.routes import bookings as booking_routes
from app.presentation.routes import environments as environment_routes

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_BASE = datetime(2999, 7, 1, tzinfo=timezone.utc)
_ROWS = 150          # three loaded pages
_CHILD_LIMIT = 4     # effective child limit for these tests (app.state, as set at startup)
_ZERO = "0" * 16     # a version no row has: every requested row counts as changed
_MAX = settings.RECONCILE_MAX_IDS
_CATALOGS = [
    (booking_routes._image_repo, "list_active"), (booking_routes._hw_config_repo, "list_active"),
    (booking_routes._namespace_repo, "list_available"),
    (booking_routes._static_vm_repo, "list_available"), (booking_routes._role_repo, "list_active"),
    (environment_routes._blueprint_repo, "list_active"),
    (environment_routes._namespace_repo, "list_available"),
    (environment_routes._namespace_repo, "list_held_standalone_by_user"),
]


def _user(row: UserModel) -> User:
    return User(id=row.id, username=row.username, password_hash="", role=row.role,
                is_active=True, created_at=_BASE)


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(async_session):
    owner = UserModel(id=uuid4(), username=f"rc-{uuid4().hex[:8]}", password_hash="x", role="user")
    other = UserModel(id=uuid4(), username=f"rc-{uuid4().hex[:8]}", password_hash="x", role="user")
    async_session.add_all([owner, other])
    await async_session.flush()
    common = {"ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT}
    statuses = ["QUEUED", "PROVISIONING", "READY", "FAILED", "READY"]
    bookings = [{**common, "id": uuid4(), "user_id": str(owner.id), "resource_type": "VM",
                 "status": statuses[i % len(statuses)], "image_name": "ubuntu",
                 "vm_password": "never-in-html", "created_at": _BASE - timedelta(seconds=i)}
                for i in range(_ROWS)]
    foreign = {**common, "id": uuid4(), "user_id": str(other.id), "resource_type": "VM",
               "status": "READY", "created_at": _BASE - timedelta(seconds=_ROWS)}
    await async_session.execute(insert(BookingModel), bookings + [foreign])

    envs, children = [], []

    def env(i, n_children):
        env_id = uuid4()
        envs.append({**common, "id": env_id, "name": f"env-{i}", "user_id": str(owner.id),
                     "construction_complete": True, "created_at": _BASE - timedelta(seconds=i)})
        children.extend({**common, "id": uuid4(), "user_id": str(owner.id), "resource_type": "NAMESPACE",
                         "status": "READY", "environment_id": env_id,
                         "created_at": _BASE - timedelta(seconds=i, milliseconds=k)}
                        for k in range(n_children))
        return env_id

    env_ids = [env(i, 2) for i in range(_ROWS)]
    at_limit = env(_ROWS, _CHILD_LIMIT)
    over_limit = env(_ROWS + 1, _CHILD_LIMIT + 5)   # as if inserted directly into the database
    await async_session.execute(insert(EnvironmentModel), envs)
    await async_session.execute(insert(BookingModel), children)
    await async_session.flush()
    return {"owner": _user(owner), "bookings": [b["id"] for b in bookings], "foreign": foreign["id"],
            "envs": env_ids, "at_limit": at_limit, "over_limit": over_limit}


@pytest.fixture(autouse=True)
def _app_state():
    app.state.environment_child_limit = _CHILD_LIMIT
    yield
    del app.state.environment_child_limit
    app.dependency_overrides.clear()


async def _measure(session, user, url):
    async def _session():
        yield session

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[require_user] = lambda: user
    statements, child_rows = [], []
    real_child = environment_repo._to_child_item

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    def _child(row):
        child_rows.append(row.environment_id)
        return real_child(row)

    engine = session.bind.engine.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        with ExitStack() as stack:
            spies = []
            for repo, method in _CATALOGS:
                spy = AsyncMock(wraps=getattr(repo, method))
                stack.enter_context(patch.object(repo, method, new=spy))
                spies.append(spy)
            stack.enter_context(patch.object(environment_repo, "_to_child_item", side_effect=_child))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert response.status_code == 200, response.text[:500]
    return {"statements": len(statements), "bytes": len(response.content),
            "catalog_calls": sum(s.await_count for s in spies), "child_rows": child_rows,
            "html": response.text}


def _query(ids, versions=None):
    versions = versions or {}
    return "&".join(f"r={i}.{versions.get(str(i), _ZERO)}" for i in ids)


def _versions(html):
    return dict(re.findall(r'id="(?:booking|environment)-([0-9a-f-]+)"[^>]*?data-row-version="([0-9a-f]+)"',
                           html, flags=re.S))


async def test_bookings_reconcile_cost_is_fixed(async_session, seeded):
    ids = seeded["bookings"]
    batches = {"1 id": ids[:1], f"{_MAX} ids": ids[:_MAX]}
    batches.update({f"rotation {k + 1}/3": ids[k * _MAX:(k + 1) * _MAX] for k in range(3)})
    counts = {}
    for name, batch in batches.items():
        m = await _measure(async_session, seeded["owner"], f"/book/vm/reconcile?filter=mine&{_query(batch)}")
        counts[name] = m["statements"]
        assert m["catalog_calls"] == 0
        assert "never-in-html" not in m["html"]
        print(f"\n[#497 cost] bookings  {name:<14} statements={m['statements']} bytes={m['bytes']}")
    assert set(counts.values()) == {max(counts.values())}, counts
    assert max(counts.values()) <= 7


async def test_bookings_unchanged_batch_is_small(async_session, seeded):
    batch = seeded["bookings"][:_MAX]
    changed = await _measure(async_session, seeded["owner"], f"/book/vm/reconcile?{_query(batch)}")
    versions = _versions(changed["html"])
    assert len(versions) == _MAX
    unchanged = await _measure(async_session, seeded["owner"],
                               f"/book/vm/reconcile?{_query(batch, versions)}")
    print(f"\n[#497 cost] bookings  {_MAX} rows changed={changed['bytes']} bytes"
          f" unchanged={unchanged['bytes']} bytes")
    assert unchanged["bytes"] < changed["bytes"] / 20
    assert unchanged["statements"] == changed["statements"]
    assert not _versions(unchanged["html"])


async def test_environments_reconcile_cost_is_fixed(async_session, seeded):
    ids = seeded["envs"]
    batches = {"1 id": ids[:1], f"{_MAX} ids": ids[:_MAX]}
    batches.update({f"rotation {k + 1}/3": ids[k * _MAX:(k + 1) * _MAX] for k in range(3)})
    counts = {}
    for name, batch in batches.items():
        m = await _measure(async_session, seeded["owner"], f"/environments/reconcile?{_query(batch)}")
        counts[name] = m["statements"]
        assert m["catalog_calls"] == 0
        print(f"\n[#497 cost] envs      {name:<14} statements={m['statements']} bytes={m['bytes']}")
    assert set(counts.values()) == {max(counts.values())}, counts
    assert max(counts.values()) <= 5


async def test_environment_at_the_limit_reads_at_most_limit_plus_one(async_session, seeded):
    m = await _measure(async_session, seeded["owner"],
                       f"/environments/reconcile?{_query([seeded['at_limit']])}")
    assert len(m["child_rows"]) <= _CHILD_LIMIT + 1
    assert m["html"].count("status-READY") >= _CHILD_LIMIT     # rendered with every child


async def test_environment_over_the_limit_stays_within_the_bounds(async_session, seeded):
    batch = [seeded["over_limit"], *seeded["envs"][:3]]
    m = await _measure(async_session, seeded["owner"], f"/environments/reconcile?{_query(batch)}")
    assert m["statements"] <= 5
    assert m["child_rows"].count(seeded["over_limit"]) <= _CHILD_LIMIT + 1
    assert "could not be refreshed" in m["html"]
    for env_id in seeded["envs"][:3]:                            # the rest is reconciled normally
        assert f'id="environment-{env_id}"' in m["html"]


# ── 8.2: authorization and filter changes in flight ───────────────────────────
async def test_mine_request_removes_a_row_the_viewer_may_not_see(async_session, seeded):
    m = await _measure(async_session, seeded["owner"],
                       f"/book/vm/reconcile?filter=mine&{_query([seeded['foreign']])}")
    assert f'<tr id="booking-{seeded["foreign"]}" hx-swap-oob="delete"></tr>' in m["html"]


async def test_request_built_for_all_is_answered_with_all_visibility(async_session, seeded):
    # A request the All section sent just before the user switched to Mine: the server answers it as
    # an All request (it is stateless) — the client discards it because its section is gone.
    m = await _measure(async_session, seeded["owner"],
                       f"/book/vm/reconcile?filter=all&{_query([seeded['foreign']])}")
    assert re.search(rf'<tr\s+id="booking-{seeded["foreign"]}"[^>]*hx-swap-oob="true"', m["html"])
    assert 'hx-swap-oob="delete"' not in m["html"]
