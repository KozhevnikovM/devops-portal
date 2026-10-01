"""Integration: per-owner page walks and the plan-stable released check of the environments list
(#496, openspec change environment-list-owner-walks).

One committed, analysed dataset (design.md Decision 6), built in this order: VACUUM (clears dead
index entries left by earlier rolled-back tests), seed, commit, VACUUM ANALYZE — so plans are made
on statistics of the seeded data. It has a heavy owner, a dispatcher with owner = creator rows, a
low-share user, a rare user with old environments only, a crowd of other owners, mostly fully
released history, empty and mixed-status environments, and 1–6 children each.

Checked:
- plan shape of the page-keys query (5.2), under custom and generic plans;
- Mine read bounded by the viewer's own history for low-share and rare users (5.3), with current
  statistics — the spec's conditional bound, not a guarantee under stale statistics;
- the released check is a per-environment lookup on ix_bookings_environment_id, never a hash
  (5.4), including with a large work_mem;
- full traversal equals the unpaginated list (5.5), also with the viewer-keyed indexes dropped and
  under generic plans.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.domain.pagination import KeysetCursor
from app.infrastructure.database.models import BookingModel, EnvironmentModel
from app.infrastructure.repositories._ordered_walk import _OrderedWalk
from app.infrastructure.repositories.environment_repo import (
    EnvironmentRepository,
    _list_stmt,
    _page_keys_stmt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_repo = EnvironmentRepository()
_N = 8000
_LIMIT = 50
_PAGE_INDEXES = {
    "ix_environments_created_at_id", "ix_environments_owner_page", "ix_environments_creator_page",
}
_VIEWER_KEYED = {"ix_environments_owner_page", "ix_environments_creator_page"}
_LABELS = {"none": None, "dense": "web", "sparse": "needle"}
_PLAN_MODES = ["force_custom_plan", "force_generic_plan"]
# Long before any other test's rows (they use year 2999), so this history is the oldest.
_BASE = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Dataset:
    heavy: str
    dispatcher: str
    low: str
    rare: str
    env_ids: list[UUID]
    seeded_at: datetime

    def user(self, who: str) -> str | None:
        return None if who == "all" else getattr(self, who)


def _children(i: int, owner: str) -> list[dict]:
    """1 % empty; else 1–6 children: ~83 % fully released, ~8 % active, ~8 % mixed."""
    if i % 100 == 7:
        return []
    n = 1 + i % 6
    if i % 12 == 0:
        statuses = [(S.READY, S.RELEASED, S.FAILED)[k % 3] for k in range(2 + (i // 12) % 5)]
    elif i % 12 == 6:
        statuses = [S.READY] * n
    else:
        statuses = [S.RELEASED] * n
    return [{
        "id": uuid4(), "user_id": owner, "status": s.value, "resource_type": "VM",
        "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT,
    } for s in statuses]


def _owner_and_creator(i: int, ds: dict) -> tuple[str, str | None]:
    if i < 20:
        return ds["rare"], None                      # only old environments
    r = i % 200
    if r < 70:
        return ds["heavy"], None                     # 35 %
    if r == 70:
        return ds["low"], None                       # 0.5 %
    if r == 71:
        return ds["dispatcher"], ds["dispatcher"]    # owner = creator
    if r < 75:
        return ds["dispatcher"], None
    crowd = f"{ds['tag']}-crowd{i % 40}"
    if r < 81:
        return crowd, ds["dispatcher"]               # dispatched on someone's behalf
    if r == 81 and i % 400 == 81:
        return crowd, ds["low"]
    return crowd, None


def _name(i: int) -> str:
    if i % 997 == 500:
        return f"needle-{i}"
    return f"web-{i}" if i % 3 == 0 else f"svc-{i}"


async def _vacuum(engine: AsyncEngine, sql: str) -> None:
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text(sql))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def dataset(async_engine: AsyncEngine):
    tag = f"e496{uuid4().hex[:8]}"
    ds = {"tag": tag, "heavy": f"{tag}-heavy", "dispatcher": f"{tag}-disp",
          "low": f"{tag}-low", "rare": f"{tag}-rare"}
    await _vacuum(async_engine, "VACUUM environments, bookings")

    env_ids: list[UUID] = []
    env_rows, child_rows = [], []
    for i in range(_N):
        env_id = uuid4()
        env_ids.append(env_id)
        owner, creator = _owner_and_creator(i, ds)
        # Every 50th environment shares its predecessor's created_at: id breaks the tie.
        created_at = _BASE + timedelta(seconds=i - (i % 50 == 1))
        env_rows.append({
            "id": env_id, "name": _name(i), "user_id": owner, "created_by": creator,
            "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT, "construction_complete": True,
            "created_at": created_at,
        })
        for child in _children(i, owner):
            child_rows.append({**child, "environment_id": env_id})
    async with AsyncSession(async_engine) as session:
        await session.execute(insert(EnvironmentModel), env_rows)
        await session.execute(insert(BookingModel), child_rows)
        await session.commit()
        seeded_at: datetime = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
    await _vacuum(async_engine, "VACUUM ANALYZE environments, bookings")

    yield Dataset(heavy=ds["heavy"], dispatcher=ds["dispatcher"], low=ds["low"],
                  rare=ds["rare"], env_ids=env_ids, seeded_at=seeded_at)

    async with AsyncSession(async_engine) as session:
        for start in range(0, len(env_ids), 1000):
            chunk = env_ids[start:start + 1000]
            await session.execute(delete(BookingModel).where(BookingModel.environment_id.in_(chunk)))
            await session.execute(delete(EnvironmentModel).where(EnvironmentModel.id.in_(chunk)))
        await session.commit()


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────

async def _unpaginated(session, user_id, *, label=None, include_released=True) -> list:
    rows = (await session.execute(
        _list_stmt(user_id, label=label, include_released=include_released)
    )).all()
    return [(m.created_at, m.id) for m, _, _ in rows]


async def _deep_cursor(session, user_id) -> KeysetCursor:
    """The middle of the viewer's unfiltered list."""
    keys = await _unpaginated(session, user_id)
    created_at, env_id = keys[len(keys) // 2]
    return KeysetCursor(created_at=created_at, id=env_id)


def _literal(value) -> str:
    return "'" + (value.isoformat() if isinstance(value, datetime) else str(value)).replace("'", "''") + "'"


async def _explain_keys(session, stmt, *, plan_mode="force_custom_plan", pinned=True) -> dict:
    """EXPLAIN ANALYZE the statement as a PREPAREd statement under `plan_mode`, inside the app's
    ordered-walk pin unless `pinned` is False. Returns the top plan node's JSON."""
    compiled = stmt.compile(dialect=asyncpg_dialect())
    args = ", ".join(_literal(compiled.params[name]) for name in compiled.positiontup)
    await session.execute(text(f"SET LOCAL plan_cache_mode = {plan_mode}"))
    await session.execute(text(f"PREPARE keys_496 AS {compiled}"))
    try:
        explain = text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE keys_496({args})")
        if pinned:
            async with _OrderedWalk(session):
                [plan] = (await session.execute(explain)).scalars()
        else:
            [plan] = (await session.execute(explain)).scalars()
    finally:
        await session.execute(text("DEALLOCATE keys_496"))
        await session.execute(text("RESET plan_cache_mode"))
    plan = plan if isinstance(plan, list) else json.loads(plan)
    return plan[0]


def _nodes(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _env_scans(plan: dict) -> list[dict]:
    return [n for n in _nodes(plan["Plan"]) if n.get("Relation Name") == "environments"]


def _env_rows_read(plan: dict) -> int:
    return sum(
        (n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"]
        for n in _env_scans(plan)
    )


def _keys(user_id, *, label, include_released, after, limit=_LIMIT):
    return _page_keys_stmt(
        user_id, label=label, include_released=include_released, limit=limit, after=after,
    )


async def _own_history(session, user_id) -> int:
    return (await session.execute(text(
        "SELECT (SELECT count(*) FROM environments WHERE user_id = :u)"
        " + (SELECT count(*) FROM environments WHERE created_by = :u)"
    ), {"u": user_id})).scalar_one()


# ── 5.1: the fixture ─────────────────────────────────────────────────────────────────────────

async def test_fixture_is_seeded_and_analysed_after_the_seed(async_session, dataset):
    stats = (await async_session.execute(text(
        "SELECT relname, last_analyze, last_vacuum FROM pg_stat_user_tables"
        " WHERE relname IN ('environments', 'bookings')"
    ))).all()
    assert {r.relname for r in stats} == {"environments", "bookings"}
    for r in stats:
        assert r.last_analyze is not None and r.last_analyze >= dataset.seeded_at, r
    counts = dict((await async_session.execute(text(
        "SELECT user_id, count(*) FROM environments WHERE user_id IN (:h, :d, :l, :r) GROUP BY user_id"
    ), {"h": dataset.heavy, "d": dataset.dispatcher, "l": dataset.low, "r": dataset.rare})).all())
    assert counts[dataset.heavy] > 0.3 * _N
    assert counts[dataset.low] < 0.01 * _N
    assert counts[dataset.rare] == 20
    owner_is_creator = (await async_session.execute(text(
        "SELECT count(*) FROM environments WHERE user_id = :d AND created_by = :d"
    ), {"d": dataset.dispatcher})).scalar_one()
    assert owner_is_creator > 0


# ── 5.2: plan shape of the page-keys query ───────────────────────────────────────────────────

@pytest.mark.parametrize("plan_mode", _PLAN_MODES)
@pytest.mark.parametrize("deep", [False, True], ids=["first-page", "deep-cursor"])
@pytest.mark.parametrize("include_released", [True, False], ids=["show-released", "hide-released"])
@pytest.mark.parametrize("label", list(_LABELS), ids=[f"label-{k}" for k in _LABELS])
@pytest.mark.parametrize("who", ["all", "heavy", "dispatcher", "low"])
async def test_page_keys_read_environments_in_page_order_through_an_index(
    async_session, dataset, who, label, include_released, deep, plan_mode,
):
    user_id = dataset.user(who)
    after = await _deep_cursor(async_session, user_id) if deep else None
    plan = await _explain_keys(
        async_session,
        _keys(user_id, label=_LABELS[label], include_released=include_released, after=after),
        plan_mode=plan_mode,
    )

    scans = _env_scans(plan)
    assert scans, plan
    for scan in scans:
        assert scan["Node Type"] in ("Index Scan", "Index Only Scan"), scan
        assert scan["Index Name"] in _PAGE_INDEXES, scan
        assert scan["Scan Direction"] == "Backward", scan
        if after is not None:
            # The cursor is an index condition: environments before it are never read.
            assert "ROW(created_at, id) < ROW(" in scan.get("Index Cond", ""), scan
    node_types = {n["Node Type"] for n in _nodes(plan["Plan"])}
    assert not node_types & {"Seq Scan", "Bitmap Heap Scan", "Sort", "Incremental Sort"}, node_types
    assert "hashed SubPlan" not in json.dumps(plan)
    assert "JIT" not in plan


# ── 5.3: Mine bounded by the viewer's own history (current statistics) ────────────────────────

@pytest.mark.parametrize("plan_mode", _PLAN_MODES)
@pytest.mark.parametrize("label", [None, "needle"], ids=["no-label", "sparse-label"])
@pytest.mark.parametrize("include_released", [True, False], ids=["show-released", "hide-released"])
@pytest.mark.parametrize("who", ["low", "rare"])
async def test_mine_for_a_small_share_user_reads_only_their_own_history(
    async_session, dataset, who, include_released, label, plan_mode,
):
    user_id = dataset.user(who)
    plan = await _explain_keys(
        async_session,
        _keys(user_id, label=label, include_released=include_released, after=None),
        plan_mode=plan_mode,
    )

    for scan in _env_scans(plan):
        assert scan["Index Name"] in _VIEWER_KEYED, scan
        cond = scan["Index Cond"]
        assert "user_id" in cond or "created_by" in cond, scan
    assert _env_rows_read(plan) <= await _own_history(async_session, user_id), plan


async def test_sparse_label_matching_none_of_the_viewers_is_an_empty_page(async_session, dataset):
    """Spec: "Sparse label reads its scope only"."""
    others = await _unpaginated(async_session, None, label="needle")
    assert others, "needle must match other users' environments"
    page = await _repo.list_page(
        async_session, user_id=dataset.low, label="needle", include_released=True,
        limit=_LIMIT, after=None,
    )
    assert page.items == [] and page.next_cursor is None


# ── 5.4: the released check is a per-environment lookup ───────────────────────────────────────

# Correlated on the outer environment (aliased environments_N inside the Mine union).
_CHILD_LOOKUP = re.compile(r"\(environment_id = environments(_\d+)?\.id\)")


def _assert_released_check_is_a_child_lookup(plan: dict, *, pinned: bool) -> None:
    """Each environment's check looks up its own children on ix_bookings_environment_id. Pinned
    (the page), that is a plain index scan. Unpinned (the JSON list, or a page plan without the
    pin) PostgreSQL may run the same per-environment lookup as a bitmap of that environment's
    children; what it must never do is read or hash all (unreleased) bookings."""
    assert "hashed SubPlan" not in json.dumps(plan)
    nodes = list(_nodes(plan["Plan"]))
    assert not [n for n in nodes if n.get("Relation Name") == "bookings" and n["Node Type"] == "Seq Scan"]
    allowed = ("Index Scan", "Index Only Scan") if pinned else (
        "Index Scan", "Index Only Scan", "Bitmap Index Scan")
    lookups = [n for n in nodes if n.get("Index Name", "").startswith("ix_bookings")]
    assert lookups, plan
    for scan in lookups:
        assert scan["Node Type"] in allowed, scan
        assert scan["Index Name"] == "ix_bookings_environment_id", scan
        assert _CHILD_LOOKUP.fullmatch(scan["Index Cond"]), scan
    if pinned:
        assert not [n for n in nodes if n["Node Type"] == "Bitmap Heap Scan"], plan


@pytest.mark.parametrize("work_mem", [None, "256MB"], ids=["default-work-mem", "large-work-mem"])
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "unpinned"])
@pytest.mark.parametrize("deep", [False, True], ids=["first-page", "deep-cursor"])
@pytest.mark.parametrize("who", ["all", "heavy", "low"])
async def test_released_check_never_reads_all_bookings(async_session, dataset, who, deep, pinned, work_mem):
    user_id = dataset.user(who)
    after = await _deep_cursor(async_session, user_id) if deep else None
    if work_mem is not None:
        await async_session.execute(text(f"SET LOCAL work_mem = '{work_mem}'"))
    plan = await _explain_keys(
        async_session, _keys(user_id, label=None, include_released=False, after=after),
        pinned=pinned,
    )
    _assert_released_check_is_a_child_lookup(plan, pinned=pinned)


@pytest.mark.parametrize("work_mem", [None, "256MB"], ids=["default-work-mem", "large-work-mem"])
async def test_unpaginated_list_released_check_never_reads_all_bookings(async_session, dataset, work_mem):
    """The JSON list keeps its single unpinned statement but gets the same predicate."""
    if work_mem is not None:
        await async_session.execute(text(f"SET LOCAL work_mem = '{work_mem}'"))
    plan = await _explain_keys(
        async_session, _list_stmt(dataset.heavy, label=None, include_released=False), pinned=False,
    )
    _assert_released_check_is_a_child_lookup(plan, pinned=False)


# ── 5.5: full traversal equality ─────────────────────────────────────────────────────────────

async def _traverse(session, user_id, *, label, include_released, limit):
    """Follow next_cursor to the end; return the pages as (keys, cursor) pairs."""
    pages, after = [], None
    while True:
        page = await _repo.list_page(
            session, user_id=user_id, label=label, include_released=include_released,
            limit=limit, after=after,
        )
        assert len(page.items) <= limit
        pages.append(([(e.created_at, e.id) for e in page.items], page.next_cursor))
        if page.next_cursor is None:
            return pages
        assert page.next_cursor == KeysetCursor(page.items[-1].created_at, page.items[-1].id)
        after = page.next_cursor


def _flat(pages) -> list:
    return [k for keys, _ in pages for k in keys]


@pytest.mark.parametrize("include_released", [True, False], ids=["show-released", "hide-released"])
@pytest.mark.parametrize("label", list(_LABELS), ids=[f"label-{k}" for k in _LABELS])
@pytest.mark.parametrize("who", ["all", "heavy", "dispatcher", "low", "rare"])
async def test_full_traversal_equals_the_unpaginated_list(async_session, dataset, who, label, include_released):
    user_id = dataset.user(who)
    limit = 200 if who in ("all", "heavy") else 7
    expected = await _unpaginated(
        async_session, user_id, label=_LABELS[label], include_released=include_released,
    )
    pages = await _traverse(
        async_session, user_id, label=_LABELS[label], include_released=include_released, limit=limit,
    )
    flat = _flat(pages)
    assert flat == expected
    assert len(set(flat)) == len(flat)   # owner = creator rows appear once
    assert all(len(keys) == limit for keys, _ in pages[:-1])   # full pages


async def test_dispatcher_owned_and_dispatched_environments_are_listed_once(async_session, dataset):
    both = set((await async_session.execute(
        select(EnvironmentModel.id).where(
            EnvironmentModel.user_id == dataset.dispatcher,
            EnvironmentModel.created_by == dataset.dispatcher,
        )
    )).scalars())
    assert both
    pages = await _traverse(async_session, dataset.dispatcher, label=None, include_released=True, limit=7)
    ids = [env_id for _, env_id in _flat(pages)]
    assert all(ids.count(env_id) == 1 for env_id in both)


async def test_empty_and_mixed_status_environments_follow_the_derived_status(async_session, dataset):
    from app.domain.environment_status import derive_environment_status
    pages = await _traverse(async_session, dataset.heavy, label=None, include_released=False, limit=200)
    shown = {env_id for _, env_id in _flat(pages)}
    envs = await _repo._list(async_session, dataset.heavy, include_released=True)
    for env in envs:
        statuses = [b.status for b in env.bookings]
        assert (env.id in shown) is (derive_environment_status(statuses) != S.RELEASED), statuses
    assert any(not e.bookings for e in envs if e.id in shown)                        # empty kept
    assert any(len({b.status for b in e.bookings}) > 1 for e in envs if e.id in shown)  # mixed kept


@pytest.mark.parametrize("include_released", [True, False], ids=["show-released", "hide-released"])
@pytest.mark.parametrize("label", [None, "web"], ids=["no-label", "label"])
@pytest.mark.parametrize("who", ["heavy", "low"])
async def test_mine_pages_are_the_same_without_the_viewer_keyed_indexes(
    async_session, dataset, who, label, include_released,
):
    """Spec: "Mine pages are the same on either path" — the global page-order walk with the
    viewer as a filter returns the same pages and cursors."""
    user_id = dataset.user(who)
    limit = 200 if who == "heavy" else 7
    keyed = await _traverse(async_session, user_id, label=label, include_released=include_released, limit=limit)
    plan = await _explain_keys(async_session, _keys(user_id, label=label, include_released=include_released, after=None))
    if who == "low":
        assert {s["Index Name"] for s in _env_scans(plan)} <= _VIEWER_KEYED, plan

    # Inside the test transaction only; rolled back with it.
    await async_session.execute(text("DROP INDEX ix_environments_owner_page"))
    await async_session.execute(text("DROP INDEX ix_environments_creator_page"))
    plan = await _explain_keys(async_session, _keys(user_id, label=label, include_released=include_released, after=None))
    assert {s["Index Name"] for s in _env_scans(plan)} == {"ix_environments_created_at_id"}, plan
    global_walk = await _traverse(
        async_session, user_id, label=label, include_released=include_released, limit=limit,
    )
    assert global_walk == keyed


@pytest.mark.parametrize("include_released", [True, False], ids=["show-released", "hide-released"])
@pytest.mark.parametrize("label", list(_LABELS), ids=[f"label-{k}" for k in _LABELS])
@pytest.mark.parametrize("who", ["all", "dispatcher", "low"])
async def test_custom_and_generic_plans_give_the_same_pages(async_session, dataset, who, label, include_released):
    user_id = dataset.user(who)
    limit = 200 if who == "all" else 7
    custom = await _traverse(
        async_session, user_id, label=_LABELS[label], include_released=include_released, limit=limit,
    )
    await async_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    generic = await _traverse(
        async_session, user_id, label=_LABELS[label], include_released=include_released, limit=limit,
    )
    assert generic == custom


# ── 3.2: model / migration index parity ───────────────────────────────────────────────────────

async def test_model_and_migration_define_identical_environment_page_indexes(async_session):
    """The migrated indexes (conftest ran 0036) are exactly what the model declares — compared as
    PostgreSQL stores them: names, columns and the creator index's partial predicate."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    def canonical(defs):
        return {name: d.split(" USING ", 1)[1] for name, d in defs}

    migrated = canonical((await async_session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'environments'"
        " AND indexname LIKE 'ix_environments_%'"
    ))).all())
    await async_session.execute(text(
        "CREATE TEMP TABLE environments_model (LIKE environments INCLUDING DEFAULTS) ON COMMIT DROP"
    ))
    for index in EnvironmentModel.__table__.indexes:
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        await async_session.execute(text(ddl.replace(" ON environments ", " ON environments_model ", 1)))
    declared = canonical((await async_session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'environments_model'"
    ))).all())
    assert declared == migrated
    assert migrated["ix_environments_owner_page"] == "btree (user_id, created_at, id)"
    assert migrated["ix_environments_creator_page"] == (
        "btree (created_by, created_at, id) WHERE (created_by IS NOT NULL)"
    )
