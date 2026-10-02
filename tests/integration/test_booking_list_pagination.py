"""Integration: keyset pagination of the browser bookings lists (#479).

`list_page` returns at most `limit` bookings ordered (created_at DESC, id DESC) and continues
strictly after a (created_at, id) cursor. Every traversal is checked against the unpaginated
`list_all` / `list_by_user` with the same filters, so pagination can't drop, repeat or reorder rows.

Guarantee (design.md, Decisions 2, 3, 9 and 10): without a label, page selection reads at most
4 × (limit + 1) booking index entries on the plan the page query runs — pinned by `list_page` to
the ordered index walks; plans here are taken under that same pin, never a setting of the test's
own — however much RELEASED / FAILED history other users or other resource types have. With a
label (#485) it examines a window of at most the scan size S: at most 4 × (S + 1) index entries
and S label tests, however sparse the label. Queue rank reads only QUEUED rows.
"""
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.domain.pagination import KeysetCursor
from app.infrastructure.database.models import BookingModel
from app.infrastructure.repositories._ordered_walk import _OrderedWalk
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _label_page_keys_stmt,
    _page_keys_stmt,
    _queue_rank_stmt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_repo = BookingRepository()
_VM_TYPES = ["VM", "STATIC_VM"]
_NS_TYPES = ["NAMESPACE"]
# The label scan size (#485) for tests that don't exercise it: larger than any seeded range.
_SCAN = 200
# Far in the future so seeded rows sort before any other booking in the test database.
_BASE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _row(owner: str, created_at: datetime, *, status=S.READY, rtype="VM", label=None,
         created_by=None) -> dict:
    return {
        "id": uuid4(), "user_id": owner, "status": status.value, "resource_type": rtype,
        "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT, "created_at": created_at,
        "label": label, "created_by": created_by,
    }


async def _seed(session: AsyncSession, rows: list[dict]) -> list[UUID]:
    if rows:
        await session.execute(insert(BookingModel), rows)
        await session.flush()
    return [r["id"] for r in rows]


def _spaced(owner: str, n: int, step=timedelta(seconds=1), start=_BASE, **kw) -> list[dict]:
    """n rows, newest first, `step` apart."""
    return [_row(owner, start - i * step, **kw) for i in range(n)]


async def _traverse(session, *, limit, user_id, resource_types=_VM_TYPES, label=None,
                    include_released=True, scan_size=_SCAN):
    """Follow next_cursor from the first page to the last; return the pages' id lists."""
    pages, after = [], None
    while True:
        page = await _repo.list_page(
            session, user_id=user_id, resource_types=resource_types, label=label,
            include_released=include_released, limit=limit, scan_size=scan_size, after=after,
        )
        assert len(page.items) <= limit
        pages.append([b.id for b in page.items])
        if page.next_cursor is None:
            return pages
        if label is None:
            # An unlabelled page is full and continues after the last row it shows.
            assert len(page.items) == limit
            assert page.next_cursor == KeysetCursor(page.items[-1].created_at, page.items[-1].id)
        elif page.items:
            # A label page continues after its last row, or — when its scan ran out (#485) —
            # after the last booking it examined; never before a row it shows.
            last = page.items[-1]
            assert (page.next_cursor.created_at, page.next_cursor.id) <= (last.created_at, last.id)
        after = page.next_cursor


async def _unpaginated(session, *, user_id, resource_types=_VM_TYPES, label=None,
                       include_released=True) -> list[UUID]:
    kw = {"include_released": include_released, "resource_type": resource_types, "label": label}
    items = await (_repo.list_all(session, **kw) if user_id is None
                   else _repo.list_by_user(session, user_id, **kw))
    return [b.id for b in items]


def _flat(pages):
    return [i for p in pages for i in p]


# ── 4.1: page bound, cursor boundaries, ties, traversal ──────────────────────────────────────

@pytest.mark.parametrize("n, first_page, has_more", [
    pytest.param(4, 4, False, id="fewer-than-a-page"),
    pytest.param(5, 5, False, id="exactly-a-page"),
    pytest.param(6, 5, True, id="one-more-than-a-page"),
])
async def test_page_bound_and_next_cursor(async_session, n, first_page, has_more):
    owner = f"inttest-{uuid4()}"
    ids = await _seed(async_session, _spaced(owner, n))

    page = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=5, scan_size=_SCAN, after=None,
    )
    assert [b.id for b in page.items] == ids[:first_page]   # newest first
    assert (page.next_cursor is not None) is has_more


async def test_boundary_inside_equal_created_at_orders_by_id_desc(async_session):
    owner = f"inttest-{uuid4()}"
    # 2 newer rows, 9 sharing one timestamp (spans pages at limit=4, and both page types' branches),
    # 2 older rows.
    tie = _BASE - timedelta(seconds=10)
    newer = await _seed(async_session, _spaced(owner, 2))
    tied = await _seed(async_session, [
        _row(owner, tie, rtype="VM" if i % 2 else "STATIC_VM") for i in range(9)
    ])
    older = await _seed(async_session, _spaced(owner, 2, start=tie - timedelta(seconds=1)))

    pages = await _traverse(async_session, limit=4, user_id=owner)

    expected = newer + sorted(tied, reverse=True) + older
    assert _flat(pages) == expected
    assert len(set(_flat(pages))) == len(expected)
    assert [len(p) for p in pages] == [4, 4, 4, 1]


async def test_json_list_orders_equal_created_at_by_id_desc(async_session):
    """2.1: the JSON list shares the tiebreak, so equal timestamps list in a fixed order."""
    owner = f"inttest-{uuid4()}"
    tied = await _seed(async_session, [_row(owner, _BASE) for _ in range(5)])
    assert [b.id for b in await _repo.list_by_user(async_session, owner)] == sorted(tied, reverse=True)


async def test_cursor_keeps_microsecond_precision(async_session):
    owner = f"inttest-{uuid4()}"
    a, b = await _seed(async_session, [_row(owner, _BASE),
                                       _row(owner, _BASE - timedelta(microseconds=1))])

    first = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=1, scan_size=_SCAN, after=None,
    )
    assert [x.id for x in first.items] == [a]
    assert first.next_cursor.created_at == _BASE
    second = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=1, scan_size=_SCAN, after=first.next_cursor,
    )
    assert [x.id for x in second.items] == [b]
    assert second.next_cursor is None


async def test_full_traversal_equals_unpaginated_list(async_session):
    owner = f"inttest-{uuid4()}"
    rows = _spaced(owner, 17, step=timedelta(milliseconds=7))
    for i, r in enumerate(rows):
        r["resource_type"] = "STATIC_VM" if i % 3 == 0 else "VM"
    rows[5]["created_at"] = rows[6]["created_at"] = rows[7]["created_at"]   # a tie mid-stream
    await _seed(async_session, rows)
    for limit in (1, 3, 5, 17, 50):
        pages = await _traverse(async_session, limit=limit, user_id=owner)
        assert _flat(pages) == await _unpaginated(async_session, user_id=owner), limit


async def test_row_added_after_first_page_does_not_shift_later_pages(async_session):
    owner = f"inttest-{uuid4()}"
    ids = await _seed(async_session, _spaced(owner, 6))
    first = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=3, scan_size=_SCAN, after=None,
    )
    await _seed(async_session, [_row(owner, _BASE + timedelta(seconds=1))])   # newer
    second = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=3, scan_size=_SCAN, after=first.next_cursor,
    )
    assert [b.id for b in second.items] == ids[3:]


# ── 4.2: filters × visibility × pagination ───────────────────────────────────────────────────

async def _seed_mixed(session, token: str):
    """Interleave (in created_at order) two owners' bookings of every page type, status and label,
    plus dispatched bookings, so every filter's matches straddle page boundaries.

    `owner` is also a dispatcher: it ordered some of `other`'s bookings, and one of its own
    (created_by == user_id, so both Mine branches find it — it must still appear once).
    """
    owner, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    cycle: list[dict] = [
        {"who": owner, "status": S.READY, "rtype": "VM", "label": f"{token}-a"},
        {"who": other, "status": S.READY, "rtype": "VM", "label": f"{token}-b"},
        {"who": owner, "status": S.RELEASED, "rtype": "STATIC_VM", "label": f"{token}-c"},
        {"who": owner, "status": S.FAILED, "rtype": "VM", "label": "unrelated"},
        {"who": other, "status": S.RELEASED, "rtype": "NAMESPACE", "label": f"{token}-d"},
        {"who": owner, "status": S.QUEUED, "rtype": "NAMESPACE", "label": f"{token}-e"},
        {"who": other, "status": S.FAILED, "rtype": "STATIC_VM", "label": f"{token}-f", "created_by": owner},
        {"who": owner, "status": S.READY, "rtype": "VM", "label": f"{token}-g", "created_by": owner},
        {"who": other, "status": S.RELEASED, "rtype": "VM", "label": "unrelated", "created_by": owner},
        {"who": owner, "status": S.FAILED, "rtype": "NAMESPACE", "label": f"{token}-h"},
        {"who": other, "status": S.READY, "rtype": "NAMESPACE", "label": f"{token}-i"},
        {"who": owner, "status": S.RELEASED, "rtype": "NAMESPACE", "label": f"{token}-j"},
        {"who": owner, "status": S.FAILED, "rtype": "VM", "label": None},
    ]
    rows = []
    for i in range(52):
        spec = dict(cycle[i % len(cycle)])  # a copy: "who" is popped below
        who = spec.pop("who")
        rows.append(_row(who, _BASE - timedelta(seconds=i // 2), **spec))   # pairs share a timestamp
    await _seed(session, rows)
    return owner, other, rows


@pytest.mark.parametrize("mine", [True, False], ids=["mine", "all"])
@pytest.mark.parametrize("resource_types", [_VM_TYPES, _NS_TYPES], ids=["vm-page", "namespace-page"])
@pytest.mark.parametrize("use_label", [True, False], ids=["label", "no-label"])
@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_filter_combinations_traverse_exactly_the_filtered_list(
    async_session, mine, resource_types, use_label, include_released,
):
    token = f"tok{uuid4().hex[:8]}"
    owner, other, rows = await _seed_mixed(async_session, token)
    user_id = owner if mine else None
    label = token if use_label else None
    kw = {"user_id": user_id, "resource_types": resource_types, "label": label,
          "include_released": include_released}

    expected = await _unpaginated(async_session, **kw)
    pages = await _traverse(async_session, limit=3, **kw)

    assert _flat(pages) == expected
    assert len(set(_flat(pages))) == len(expected)
    by_id = {r["id"]: r for r in rows}
    seeded = [by_id[i] for i in _flat(pages) if i in by_id]
    assert seeded, "the seeded rows must be on the pages"
    assert {r["resource_type"] for r in seeded} <= set(resource_types)
    if mine:
        assert all(owner in (r["user_id"], r["created_by"]) for r in seeded)
        if resource_types == _VM_TYPES and not use_label:
            # Dispatched on someone's behalf → listed; dispatched to oneself → listed once.
            assert any(r["user_id"] == other for r in seeded)
    else:
        assert any(r["user_id"] == other for r in seeded)   # All continues past own bookings
    if include_released:
        assert any(r["status"] == S.RELEASED.value for r in seeded)
    else:
        assert all(r["status"] != S.RELEASED.value for r in seeded)
        assert any(r["status"] == S.FAILED.value for r in seeded)   # FAILED stays listed


# ── 4.3: page selection is bounded by the page size (unforced plans) ─────────────────────────

_LIMIT = 10
_BOUND = 4 * (_LIMIT + 1)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _explain_analyze(session, sql: str) -> dict:
    [plan] = (await session.execute(text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"))).scalars()
    return (json.loads(plan) if isinstance(plan, str) else plan)[0]["Plan"]


async def _page_plan(session, stmt) -> dict:
    """The page key query's plan, under the same pin list_page applies (Decision 10)."""
    async with _OrderedWalk(session):
        return await _explain_analyze(session, _sql(stmt))


def _expected_indexes(*, mine: bool, include_released: bool) -> set[str]:
    suffix = "" if include_released else "_unreleased"
    scopes = ("owner", "creator") if mine else ("type",)
    return {f"ix_bookings_{scope}_page{suffix}" for scope in scopes}


def _visited(scan: dict) -> float:
    """Index entries a scan node visited: the rows it returned plus those its filter dropped."""
    return (scan["Actual Rows"] + scan.get("Rows Removed by Filter", 0)) * scan["Actual Loops"]


def _assert_bounded(plan: dict, *, include_released: bool, mine: bool, after,
                    types=_VM_TYPES) -> None:
    nodes = list(_nodes(plan))
    scans = [n for n in nodes if n.get("Relation Name") == "bookings"]
    assert all(n["Node Type"] in ("Index Scan", "Index Only Scan") for n in scans), plan
    assert not [n for n in nodes if n["Node Type"].startswith("Bitmap")], plan
    # Exactly one scan per branch, each on that branch's own index (design.md, Decision 2).
    assert len(scans) == len(types) * (2 if mine else 1), plan
    assert {n["Index Name"] for n in scans} == _expected_indexes(mine=mine, include_released=include_released), plan
    # Every visited entry matched: no walking past history the page doesn't show.
    assert all(n.get("Rows Removed by Filter", 0) == 0 for n in scans), plan
    visited = sum(_visited(n) for n in scans)
    assert visited <= _BOUND, (visited, plan)
    for n in nodes:
        if n["Node Type"] == "Sort":
            assert all(c["Actual Rows"] <= _BOUND for c in n.get("Plans", [])), plan
    for scan in scans:
        tag = scan["Index Name"].removeprefix("ix_bookings_").split("_page")[0][0] + (
            "" if include_released else "l")
        assert f"'{tag}:'::text" in scan["Index Cond"], scan
        if after is not None:
            assert "ROW(created_at, id) < ROW(" in scan["Index Cond"], scan


def _nodes(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


async def _seed_history(session) -> dict:
    """A skewed, ANALYZEd dataset (design.md, Decision 9).

    - `crowd`: thousands of RELEASED and FAILED bookings owned by others, newer than everything
      of the viewer's and mostly VMs, so they would dominate any walk they could leak into.
    - `viewer`: an interleaved RELEASED / FAILED / READY history of both page types, plus
      bookings it dispatched for others. Its namespace bookings are sparse among VMs.
    """
    viewer = f"inttest-{uuid4()}"
    crowd = []
    for i in range(4000):
        status = (S.RELEASED, S.FAILED)[i % 2]
        rtype = "NAMESPACE" if i % 50 == 0 else ("STATIC_VM" if i % 7 == 0 else "VM")
        crowd.append(_row(f"inttest-crowd-{i % 97}", _BASE - timedelta(milliseconds=i),
                          status=status, rtype=rtype))
    older = _BASE - timedelta(hours=1)
    mine = []
    for i in range(300):
        status = (S.RELEASED, S.FAILED, S.READY, S.FAILED, S.RELEASED)[i % 5]
        rtype = ("VM", "STATIC_VM", "VM", "NAMESPACE")[i % 4] if i % 10 else "NAMESPACE"
        mine.append(_row(viewer, older - timedelta(seconds=i), status=status, rtype=rtype))
    for i in range(40):
        mine.append(_row(f"inttest-crowd-{i}", older - timedelta(seconds=i, microseconds=5),
                         status=(S.READY, S.FAILED, S.RELEASED)[i % 3],
                         rtype=("VM", "NAMESPACE")[i % 2], created_by=viewer))
    await _seed(session, crowd + mine)
    await session.execute(text("ANALYZE bookings"))
    return {"viewer": viewer}


@pytest.mark.parametrize("mine", [True, False], ids=["mine", "all"])
@pytest.mark.parametrize("resource_types", [_VM_TYPES, _NS_TYPES], ids=["vm-page", "namespace-page"])
@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_page_selection_is_bounded_by_the_page_size(
    async_session, mine, resource_types, include_released,
):
    data = await _seed_history(async_session)
    user_id = data["viewer"] if mine else None
    first = await _repo.list_page(
        async_session, user_id=user_id, resource_types=resource_types, label=None,
        include_released=include_released, limit=_LIMIT, scan_size=_SCAN, after=None,
    )
    assert len(first.items) == _LIMIT and first.next_cursor is not None
    if not include_released:
        assert any(b.status == S.FAILED for b in first.items) or not mine

    for after in (None, first.next_cursor):
        stmt = _page_keys_stmt(
            user_id, resource_types=resource_types, include_released=include_released, limit=_LIMIT, after=after,
        )
        plan = await _page_plan(async_session, stmt)
        _assert_bounded(plan, include_released=include_released, mine=mine, after=after,
                        types=resource_types)


@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_bound_holds_when_statistics_underestimate_the_branch(async_session, include_released):
    """The case found in implementation: statistics that think a viewer's branch is tiny made the
    free planner read it whole with bitmap scan + sort. Under the pin it is still an ordered walk."""
    data = await _seed_history(async_session)          # ANALYZEd while the viewer's branch is small
    viewer = data["viewer"]
    await _seed(async_session, [                       # …then it grows well past a page, unanalyzed
        _row(viewer, _BASE - timedelta(hours=3, seconds=i), status=(S.FAILED, S.READY, S.RELEASED)[i % 3],
             rtype="NAMESPACE")
        for i in range(600)
    ])
    stmt = _page_keys_stmt(viewer, resource_types=_NS_TYPES, include_released=include_released, limit=_LIMIT, after=None)
    plan = await _page_plan(async_session, stmt)
    _assert_bounded(plan, include_released=include_released, mine=True, after=None, types=_NS_TYPES)


async def test_bound_holds_when_an_operator_disabled_index_scans(async_session):
    """Review of #486: with enable_indexscan (and enable_indexonlyscan) off beforehand, turning
    bitmap and seq scans off would leave only a sequential scan. The pin forces index scans on, so
    the key query is still the ordered prefixed walk, and the operator's values come back after."""
    data = await _seed_history(async_session)
    await async_session.execute(text("SET LOCAL enable_indexscan = off"))
    await async_session.execute(text("SET LOCAL enable_indexonlyscan = off"))

    async with _OrderedWalk(async_session):
        pinned = (await async_session.execute(text(
            "SELECT current_setting('enable_indexscan'), current_setting('enable_seqscan'),"
            " current_setting('enable_bitmapscan')"
        ))).one()
    # Index scans are guaranteed, not left to how the planner ranks all-disabled alternatives.
    assert tuple(pinned) == ("on", "off", "off")

    for mine in (True, False):
        stmt = _page_keys_stmt(data["viewer"] if mine else None, resource_types=_VM_TYPES, include_released=False, limit=_LIMIT, after=None)
        plan = await _page_plan(async_session, stmt)
        _assert_bounded(plan, include_released=False, mine=mine, after=None)

    page = await _repo.list_page(
        async_session, user_id=data["viewer"], resource_types=_VM_TYPES, label=None,
        include_released=False, limit=_LIMIT, scan_size=_SCAN, after=None,
    )
    assert len(page.items) == _LIMIT
    settings = (await async_session.execute(text(
        "SELECT current_setting('enable_indexscan'), current_setting('enable_indexonlyscan')"
    ))).one()
    assert tuple(settings) == ("off", "off")


async def test_list_page_restores_the_previous_planner_settings(async_session):
    """The pin is scoped to the key query: afterwards the request plans as before — including an
    operator's non-default value, which is restored rather than reset to the default."""
    owner = f"inttest-{uuid4()}"
    await _seed(async_session, _spaced(owner, 3))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))
    await async_session.execute(text("SET LOCAL enable_bitmapscan = on"))
    await async_session.execute(text("SET LOCAL enable_indexscan = off"))

    page = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=2, scan_size=_SCAN, after=None,
    )

    assert len(page.items) == 2
    settings = (await async_session.execute(text(
        "SELECT current_setting('enable_bitmapscan'), current_setting('enable_seqscan'),"
        " current_setting('enable_indexscan'), current_setting('enable_sort')"
    ))).one()
    assert tuple(settings) == ("on", "off", "off", "on")   # sorts, off in the pin, are back on


async def test_partial_index_is_used_under_a_generic_plan(async_session):
    """The released predicate is a literal, so even a generic prepared plan (bound owner and type,
    as asyncpg sends them) can use the RELEASED-free index (design.md, Decision 2)."""
    data = await _seed_history(async_session)
    stmt = _page_keys_stmt(
        data["viewer"], resource_types=["VM"], include_released=False,
        limit=_LIMIT, after=None,
    )
    compiled = stmt.compile(dialect=postgresql.dialect())
    names = list(compiled.params)
    sql = str(compiled)
    for i, name in enumerate(names, start=1):
        sql = sql.replace(f"%({name})s", f"${i}")
    args = ", ".join(
        str(compiled.params[n]) if isinstance(compiled.params[n], int) else f"'{compiled.params[n]}'"
        for n in names
    )
    await async_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await async_session.execute(text(f"PREPARE page_keys AS {sql}"))
    try:
        async with _OrderedWalk(async_session):
            plan = "\n".join(
                (await async_session.execute(text(f"EXPLAIN EXECUTE page_keys({args})"))).scalars()
            )
    finally:
        await async_session.execute(text("DEALLOCATE page_keys"))
    assert "$1" in sql   # owner and type really are parameters
    assert "ix_bookings_owner_page_unreleased" in plan, plan
    assert "ix_bookings_creator_page_unreleased" in plan, plan
    assert "Seq Scan" not in plan and "Bitmap" not in plan, plan


async def test_model_and_migration_define_identical_page_indexes(async_session):
    """The migrated indexes (conftest ran 0035) are exactly what the model declares — compared as
    PostgreSQL stores them, since the planner matches the queries' key expressions to these."""
    from sqlalchemy.schema import CreateIndex

    from app.infrastructure.database.models import BOOKING_PAGE_INDEXES

    def canonical(defs):
        return {name: d.split(" USING ", 1)[1] for name, d in defs}

    migrated = canonical((await async_session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'bookings'"
        " AND indexname LIKE 'ix_bookings_%_page%'"
    ))).all())
    await async_session.execute(text(
        "CREATE TEMP TABLE bookings_model (LIKE bookings INCLUDING DEFAULTS) ON COMMIT DROP"
    ))
    for index in BOOKING_PAGE_INDEXES.values():
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        await async_session.execute(text(ddl.replace(" ON bookings ", " ON bookings_model ", 1)))
    declared = canonical((await async_session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'bookings_model'"
    ))).all())
    assert declared == migrated
    assert len(declared) == 6


# Review of #486: the invariant is structural, not a planner preference. Plain-column indexes a
# walk *could* use in page order (the design's first draft) are created here and made attractive
# by the statistics; each branch must still use only its own page-key index and visit only
# matching entries.
_BROAD_INDEXES = {
    "tmp_broad_type": "(resource_type, created_at, id)",
    "tmp_broad_owner": "(user_id, resource_type, created_at, id)",
    "tmp_broad_creator": "(created_by, resource_type, created_at, id)",
}


async def _add_broad_indexes(session) -> None:
    for name, columns in _BROAD_INDEXES.items():
        await session.execute(text(f"CREATE INDEX {name} ON bookings {columns}"))


@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_mine_branches_use_only_their_own_index_when_the_type_walk_looks_cheaper(
    async_session, include_released,
):
    """The viewer owns (and dispatched) nearly every recent VM, so walking all VMs by type would
    find a page almost at once — the broad type index is the planner's natural pick. And the
    viewer's own history is almost free of RELEASED rows, so the full owner index looks as good
    as the RELEASED-free one."""
    viewer = f"inttest-{uuid4()}"
    rows = []
    for i in range(3000):
        rows.append(_row(viewer, _BASE - timedelta(milliseconds=i), rtype="VM",
                         status=S.RELEASED if i % 97 == 0 else (S.READY, S.FAILED)[i % 2],
                         created_by=viewer if i % 3 == 0 else None))
    rows += [_row(f"inttest-crowd-{i}", _BASE - timedelta(seconds=10, milliseconds=i),
                  status=S.RELEASED) for i in range(300)]
    await _seed(async_session, rows)
    await _add_broad_indexes(async_session)
    await async_session.execute(text("ANALYZE bookings"))

    for after in (None, KeysetCursor(_BASE - timedelta(seconds=1), UUID(int=0))):
        stmt = _page_keys_stmt(viewer, resource_types=["VM"], include_released=include_released, limit=_LIMIT, after=after)
        plan = await _page_plan(async_session, stmt)
        _assert_bounded(plan, include_released=include_released, mine=True, after=after, types=["VM"])


@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_mine_branches_use_only_their_own_index_under_misleading_statistics(
    async_session, include_released,
):
    """Statistics taken while the viewer had almost nothing, then the viewer's branch and a huge
    RELEASED/FAILED crowd grow unanalyzed: nothing about the estimates favours the page-key walk."""
    viewer = f"inttest-{uuid4()}"
    await _seed(async_session, [_row(viewer, _BASE - timedelta(days=2))])
    await _add_broad_indexes(async_session)
    await async_session.execute(text("ANALYZE bookings"))
    await _seed(async_session, [
        _row(f"inttest-crowd-{i % 50}", _BASE - timedelta(milliseconds=i), status=(S.RELEASED, S.FAILED)[i % 2],
             rtype="NAMESPACE") for i in range(3000)
    ] + [
        _row(viewer, _BASE - timedelta(days=1, seconds=i), status=(S.FAILED, S.READY, S.RELEASED)[i % 3],
             rtype="NAMESPACE", created_by=viewer if i % 2 else None) for i in range(400)
    ])

    stmt = _page_keys_stmt(viewer, resource_types=_NS_TYPES, include_released=include_released, limit=_LIMIT, after=None)
    plan = await _page_plan(async_session, stmt)
    _assert_bounded(plan, include_released=include_released, mine=True, after=None, types=_NS_TYPES)


@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_all_branches_use_only_their_own_index(async_session, include_released):
    """All on the namespace page, with namespaces rare among VMs and a broad plain index present:
    the walk stays on its type's page-key index."""
    rows = [_row(f"inttest-crowd-{i % 70}", _BASE - timedelta(milliseconds=i),
                 status=(S.RELEASED, S.FAILED, S.READY)[i % 3], rtype="VM") for i in range(3000)]
    rows += [_row(f"inttest-crowd-{i % 70}", _BASE - timedelta(seconds=30, milliseconds=i),
                  status=(S.RELEASED, S.READY)[i % 2], rtype="NAMESPACE") for i in range(60)]
    await _seed(async_session, rows)
    await _add_broad_indexes(async_session)
    await async_session.execute(text("ANALYZE bookings"))

    stmt = _page_keys_stmt(None, resource_types=_NS_TYPES, include_released=include_released, limit=_LIMIT, after=None)
    plan = await _page_plan(async_session, stmt)
    _assert_bounded(plan, include_released=include_released, mine=False, after=None, types=_NS_TYPES)


# ── 4.4: queue rank, and the label exception ─────────────────────────────────────────────────

async def test_queue_rank_reads_only_the_queue(async_session):
    await _seed_history(async_session)
    owner = f"inttest-{uuid4()}"
    queued = await _seed(async_session, [
        _row(owner, _BASE - timedelta(days=1, seconds=i), status=S.QUEUED, rtype="NAMESPACE")
        for i in range(5)
    ])
    await async_session.execute(text("ANALYZE bookings"))
    # The bulk rank read (#495) as the repository runs it: under the ordered-walk pin.
    stmt = _queue_rank_stmt({"NAMESPACE": _BASE}, queued)
    async with _OrderedWalk(async_session):
        plan = await _explain_analyze(async_session, _sql(stmt))
    scans = [n for n in _nodes(plan) if n.get("Relation Name") == "bookings"]
    assert [n["Index Name"] for n in scans] == ["ix_bookings_queued_rank"], plan
    total_queued = (await async_session.execute(text(
        "SELECT count(*) FROM bookings WHERE resource_type = 'NAMESPACE' AND status = 'QUEUED'"
    ))).scalar_one()
    assert total_queued >= len(queued)
    assert sum(n["Actual Rows"] * n["Actual Loops"] for n in scans) <= total_queued, plan


async def test_label_walk_stays_within_the_users_own_branches(async_session):
    """A Mine label page never leaves the viewer's owner / creator index ranges, however common the
    label is among other users."""
    await _seed_history(async_session)
    owner = f"inttest-{uuid4()}"
    token = f"tok{uuid4().hex[:8]}"
    await _seed(async_session, [
        _row(f"inttest-crowd-{i % 97}", _BASE - timedelta(milliseconds=i), label=f"{token}-x")
        for i in range(1000)
    ])
    rows = _spaced(owner, 60, start=_BASE - timedelta(hours=2))
    for i, r in enumerate(rows):
        r["label"] = f"{token}-mine" if i % 20 == 0 else "other"
    await _seed(async_session, rows)
    await async_session.execute(text("ANALYZE bookings"))

    page = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=token,
        include_released=True, limit=_LIMIT, scan_size=_SCAN, after=None,
    )
    assert [b.id for b in page.items] == [r["id"] for r in rows if r["label"] == f"{token}-mine"]

    stmt = _label_page_keys_stmt(owner, resource_types=_VM_TYPES, label=token, include_released=True,
                                 limit=_LIMIT, scan_size=_SCAN, after=None)
    plan = await _page_plan(async_session, stmt)
    scans = [n for n in _nodes(plan) if n.get("Relation Name") == "bookings"]
    walks = [n for n in scans if n["Index Name"] != "bookings_pkey"]
    assert walks and all(
        n["Node Type"] in ("Index Scan", "Index Only Scan")
        and n["Index Name"].startswith(("ix_bookings_owner_page", "ix_bookings_creator_page"))
        for n in walks
    ), plan


# ── #485: label-filtered pages are bounded by the scan size ──────────────────────────────────

_LABEL_LIMIT = 10
_LABEL_SCAN = 50
_LABEL_BOUND = 4 * (_LABEL_SCAN + 1)


async def _seed_sparse_label(session, needle: str) -> dict:
    """A large, ANALYZEd history where `needle` is sparse (task 4.1).

    - `crowd`: 4000 RELEASED / FAILED bookings owned by others, newer than the viewer's, of every
      page type; only every 997th carries `needle`, so All with the label is sparse too.
    - `viewer`: 1500 bookings of both page types, interleaved READY / FAILED / RELEASED, labelled
      "other" except a handful deep in the history; plus bookings it dispatched for others.
    """
    viewer = f"inttest-{uuid4()}"
    crowd = []
    for i in range(4000):
        rtype = "NAMESPACE" if i % 50 == 0 else ("STATIC_VM" if i % 7 == 0 else "VM")
        crowd.append(_row(f"inttest-crowd-{i % 97}", _BASE - timedelta(milliseconds=i),
                          status=(S.RELEASED, S.FAILED)[i % 2], rtype=rtype,
                          label=f"{needle}-crowd" if i % 997 == 0 else "other"))
    older = _BASE - timedelta(hours=1)
    mine = []
    deep = {1100, 1250, 1301, 1402, 1460, 1497, 1498}
    for i in range(1500):
        mine.append(_row(viewer, older - timedelta(seconds=i),
                         status=(S.RELEASED, S.FAILED, S.READY, S.FAILED, S.RELEASED)[i % 5],
                         rtype=("VM", "STATIC_VM", "VM", "NAMESPACE")[i % 4],
                         label=f"{needle}-mine" if i in deep else "other"))
    for i in range(40):
        mine.append(_row(f"inttest-crowd-{i}", older - timedelta(seconds=i, microseconds=5),
                         status=(S.READY, S.FAILED, S.RELEASED)[i % 3],
                         rtype=("VM", "NAMESPACE")[i % 2], created_by=viewer, label="other"))
    await _seed(session, crowd + mine)
    await session.execute(text("ANALYZE bookings"))
    count = (await session.execute(text(
        "SELECT count(*) FROM bookings WHERE user_id = :v"), {"v": viewer})).scalar_one()
    assert count == 1500
    matches = (await session.execute(text(
        "SELECT count(*) FROM bookings WHERE label ILIKE :p"), {"p": f"%{needle}%"})).scalar_one()
    assert matches == len(deep) + len([i for i in range(4000) if i % 997 == 0])
    return {"viewer": viewer}


def _assert_label_bounded(plan: dict, *, include_released: bool, mine: bool, after,
                          types=_VM_TYPES) -> None:
    """Design Decision 2 / task 4.2: the walks are #479's (own index, nothing filtered, cursor as
    an index condition) but bounded by S + 1; the label is tested only by pkey lookups, ≤ S."""
    nodes = list(_nodes(plan))
    scans = [n for n in nodes if n.get("Relation Name") == "bookings"]
    assert all(n["Node Type"] in ("Index Scan", "Index Only Scan") for n in scans), plan
    assert not [n for n in nodes if n["Node Type"].startswith("Bitmap")], plan
    walks = [n for n in scans if n["Index Name"] != "bookings_pkey"]
    lookups = [n for n in scans if n["Index Name"] == "bookings_pkey"]
    assert len(walks) == len(types) * (2 if mine else 1), plan
    assert {n["Index Name"] for n in walks} == _expected_indexes(mine=mine, include_released=include_released), plan
    assert all(n.get("Rows Removed by Filter", 0) == 0 and "Filter" not in n for n in walks), plan
    visited = sum(_visited(n) for n in walks)
    assert visited <= _LABEL_BOUND, (visited, plan)
    for walk in walks:
        if after is not None:
            assert "ROW(created_at, id) < ROW(" in walk["Index Cond"], walk
    # The label test: pkey lookups of the examined window only, and nowhere else.
    assert lookups, plan
    assert sum(_visited(n) for n in lookups) <= _LABEL_SCAN, plan
    label_tests = [n for n in nodes if "~~*" in n.get("Filter", "") + n.get("Join Filter", "")]
    assert label_tests and all(n in lookups for n in label_tests), plan
    for n in nodes:
        if n["Node Type"] == "Sort":
            assert all(c["Actual Rows"] <= _LABEL_BOUND for c in n.get("Plans", [])), plan


@pytest.mark.parametrize("mine", [True, False], ids=["mine", "all"])
@pytest.mark.parametrize("resource_types", [_VM_TYPES, _NS_TYPES], ids=["vm-page", "namespace-page"])
@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_sparse_label_page_selection_is_bounded_by_the_scan_size(
    async_session, mine, resource_types, include_released,
):
    needle = f"tok{uuid4().hex[:8]}"
    data = await _seed_sparse_label(async_session, needle)
    user_id = data["viewer"] if mine else None
    first = await _repo.list_page(
        async_session, user_id=user_id, resource_types=resource_types, label=needle,
        include_released=include_released, limit=_LABEL_LIMIT, scan_size=_LABEL_SCAN, after=None,
    )
    # The needle is sparse: the first window can't fill a page, but the scan continues.
    assert len(first.items) < _LABEL_LIMIT and first.next_cursor is not None
    if mine:
        assert first.items == []   # the viewer's matches are all deep in their history

    for after in (None, first.next_cursor):
        stmt = _label_page_keys_stmt(
            user_id, resource_types=resource_types, label=needle, include_released=include_released,
            limit=_LABEL_LIMIT, scan_size=_LABEL_SCAN, after=after,
        )
        plan = await _page_plan(async_session, stmt)
        _assert_label_bounded(plan, include_released=include_released, mine=mine, after=after,
                              types=resource_types)


async def test_label_plan_uses_the_partial_indexes_under_a_generic_plan(async_session):
    """Task 4.3: the literal released predicate still matches the partial indexes when the label
    query is a generic prepared plan (owner, type and label as parameters)."""
    needle = f"tok{uuid4().hex[:8]}"
    data = await _seed_sparse_label(async_session, needle)
    stmt = _label_page_keys_stmt(
        data["viewer"], resource_types=["VM"], label=needle, include_released=False,
        limit=_LABEL_LIMIT, scan_size=_LABEL_SCAN, after=None,
    )
    compiled = stmt.compile(dialect=postgresql.dialect())
    names = list(compiled.params)
    sql = str(compiled)
    for i, name in enumerate(names, start=1):
        sql = sql.replace(f"%({name})s", f"${i}")
    args = ", ".join(
        str(compiled.params[n]) if isinstance(compiled.params[n], int) else f"'{compiled.params[n]}'"
        for n in names
    )
    await async_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await async_session.execute(text(f"PREPARE label_keys AS {sql}"))
    try:
        async with _OrderedWalk(async_session):
            plan = "\n".join(
                (await async_session.execute(text(f"EXPLAIN EXECUTE label_keys({args})"))).scalars()
            )
    finally:
        await async_session.execute(text("DEALLOCATE label_keys"))
    assert "$1" in sql
    assert "ix_bookings_owner_page_unreleased" in plan, plan
    assert "ix_bookings_creator_page_unreleased" in plan, plan
    assert "bookings_pkey" in plan, plan
    assert "Seq Scan" not in plan and "Bitmap" not in plan, plan


async def test_labelled_list_page_restores_the_previous_planner_settings(async_session):
    owner = f"inttest-{uuid4()}"
    rows = _spaced(owner, 30)
    rows[4]["label"] = "needle"
    await _seed(async_session, rows)
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))
    await async_session.execute(text("SET LOCAL enable_bitmapscan = on"))
    await async_session.execute(text("SET LOCAL enable_indexscan = off"))

    page = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label="needle",
        include_released=True, limit=3, scan_size=10, after=None,
    )

    assert [b.id for b in page.items] == [rows[4]["id"]]
    settings = (await async_session.execute(text(
        "SELECT current_setting('enable_bitmapscan'), current_setting('enable_seqscan'),"
        " current_setting('enable_indexscan'), current_setting('enable_sort')"
    ))).one()
    assert tuple(settings) == ("on", "off", "off", "on")   # sorts, off in the pin, are back on


@pytest.mark.parametrize("mine", [True, False], ids=["mine", "all"])
async def test_sparse_label_traversal_equals_the_unpaginated_list(async_session, mine):
    """Task 4.4: with S = 10 and limit = 3, a sparse label is found through short and empty pages,
    each match exactly once, in page order."""
    token = f"tok{uuid4().hex[:8]}"
    owner, _other, _ = await _seed_mixed(async_session, token)
    # Stretch the owner's range well past S with non-matching bookings, matches spread through it.
    rows = _spaced(owner, 120, step=timedelta(milliseconds=3), start=_BASE - timedelta(minutes=5))
    for i, r in enumerate(rows):
        r["label"] = f"{token}-deep" if i in (0, 37, 38, 39, 40, 41, 90, 119) else "other"
    await _seed(async_session, rows)
    kw = {"user_id": owner if mine else None, "resource_types": _VM_TYPES, "label": token,
          "include_released": True}

    expected = await _unpaginated(async_session, **kw)
    pages = await _traverse(async_session, limit=3, scan_size=10, **kw)

    assert _flat(pages) == expected
    assert len(set(_flat(pages))) == len(expected)
    assert any(len(p) < 3 for p in pages[:-1]), pages   # the scan ran out before a full page
    assert any(p == [] for p in pages[:-1]), pages      # …and at least once found nothing


async def test_dense_label_pages_are_full(async_session):
    owner = f"inttest-{uuid4()}"
    rows = _spaced(owner, 25)
    for r in rows:
        r["label"] = "dense-match"
    await _seed(async_session, rows)

    pages = await _traverse(async_session, limit=3, scan_size=10, user_id=owner, label="dense")

    assert [len(p) for p in pages] == [3] * 8 + [1]
    assert _flat(pages) == [r["id"] for r in rows]


@pytest.mark.parametrize("oldest_matches", [True, False], ids=["oldest-matches", "oldest-unmatched"])
@pytest.mark.parametrize("remaining", [10, 11], ids=["exactly-S", "S-plus-one"])
async def test_window_edges(async_session, remaining, oldest_matches):
    """Task 4.5, S = 10: exactly S bookings left → no next page; S + 1 → the cursor is the S-th
    (oldest examined) booking and the next page holds the last one. A matching oldest examined
    booking is listed once; an unmatched one never; the sentinel is never a row."""
    owner = f"inttest-{uuid4()}"
    rows = _spaced(owner, remaining)
    for i, r in enumerate(rows):
        r["label"] = "edge-match" if i == 2 or (i == 9 and oldest_matches) or i == 10 else "other"
    ids = await _seed(async_session, rows)
    matching = [r["id"] for r in rows if r["label"] == "edge-match"]

    first = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label="edge",
        include_released=True, limit=3, scan_size=10, after=None,
    )
    shown = [b.id for b in first.items]
    assert shown == [i for i in ids[:10] if i in matching]
    assert (ids[9] in shown) is oldest_matches
    if remaining == 10:
        assert first.next_cursor is None
        return
    assert first.next_cursor == KeysetCursor(rows[9]["created_at"], ids[9])
    second = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label="edge",
        include_released=True, limit=3, scan_size=10, after=first.next_cursor,
    )
    assert [b.id for b in second.items] == [ids[10]]
    assert second.next_cursor is None
    assert len(set(shown + [ids[10]])) == len(matching)


# ── Review of #488: a viewer with no creator bookings among other dispatchers' history ──────

async def _seed_other_dispatchers(session, *, analyze_before_growth: bool) -> str:
    """The viewer owns a sparse-label history but dispatched nothing; other dispatchers created
    thousands of same-type bookings (RELEASED and not), so the creator page indexes are large while
    the viewer's creator branches are empty. With `analyze_before_growth`, statistics are taken
    while the creator indexes are still empty and the dispatchers' history grows unanalyzed."""
    viewer = f"inttest-{uuid4()}"
    own = [_row(viewer, _BASE - timedelta(hours=1, seconds=i),
                status=(S.READY, S.FAILED, S.RELEASED)[i % 3],
                rtype=("VM", "STATIC_VM", "NAMESPACE")[i % 3],
                label="needle-own" if i in (150, 290) else "other") for i in range(300)]
    dispatched = [_row(f"inttest-crowd-{i % 97}", _BASE - timedelta(milliseconds=i),
                       status=(S.READY, S.FAILED, S.RELEASED)[i % 3],
                       rtype=("VM", "STATIC_VM", "NAMESPACE")[i % 3],
                       created_by=f"inttest-dispatcher-{i % 5}", label="needle-crowd")
                  for i in range(5000)]
    if analyze_before_growth:
        await _seed(session, own)
        await session.execute(text("ANALYZE bookings"))
        await _seed(session, dispatched)
    else:
        await _seed(session, own + dispatched)
        await session.execute(text("ANALYZE bookings"))
    return viewer


def _assert_walks_use_own_index_conditions(plan: dict, *, include_released: bool, after,
                                           bound: int, types) -> None:
    """Every Mine branch walk — creator branches included — is an Index Cond walk of its own
    page-key index: the key (and the cursor) as index conditions, no Filter, and at most `bound`
    entries visited in total."""
    nodes = list(_nodes(plan))
    walks = [n for n in nodes
             if n.get("Relation Name") == "bookings" and n.get("Index Name") != "bookings_pkey"]
    assert all(n["Node Type"] in ("Index Scan", "Index Only Scan") for n in walks), plan
    assert not [n for n in nodes if n["Node Type"].startswith("Bitmap")], plan
    assert len(walks) == 2 * len(types), plan
    assert {n["Index Name"] for n in walks} == _expected_indexes(
        mine=True, include_released=include_released), plan
    for walk in walks:
        tag = walk["Index Name"].removeprefix("ix_bookings_").split("_page")[0][0] + (
            "" if include_released else "l")
        assert f"'{tag}:'::text" in walk.get("Index Cond", ""), walk
        assert "Filter" not in walk and walk.get("Rows Removed by Filter", 0) == 0, walk
        if after is not None:
            assert "ROW(created_at, id) < ROW(" in walk["Index Cond"], walk
    assert sum(_visited(n) for n in walks) <= bound, plan


@pytest.mark.parametrize("analyze_before_growth", [False, True], ids=["analyzed", "stale-stats"])
@pytest.mark.parametrize("resource_types", [_VM_TYPES, _NS_TYPES], ids=["vm-page", "namespace-page"])
@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_creator_walks_stay_on_their_own_index_without_creator_bookings(
    async_session, analyze_before_growth, resource_types, include_released,
):
    viewer = await _seed_other_dispatchers(async_session, analyze_before_growth=analyze_before_growth)
    cursor = KeysetCursor(_BASE - timedelta(minutes=30), UUID(int=0))
    for after in (None, cursor):
        label_stmt = _label_page_keys_stmt(
            viewer, resource_types=resource_types, label="needle", include_released=include_released,
            limit=_LABEL_LIMIT, scan_size=_LABEL_SCAN, after=after,
        )
        _assert_walks_use_own_index_conditions(
            await _page_plan(async_session, label_stmt), include_released=include_released,
            after=after, bound=_LABEL_BOUND, types=resource_types,
        )
        page_stmt = _page_keys_stmt(viewer, resource_types=resource_types,
                                    include_released=include_released, limit=_LIMIT, after=after)
        _assert_walks_use_own_index_conditions(
            await _page_plan(async_session, page_stmt), include_released=include_released,
            after=after, bound=_BOUND, types=resource_types,
        )


@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_creator_walks_stay_on_their_own_index_when_no_one_dispatched(
    async_session, include_released,
):
    """The #488 observation: no one has dispatched anything, so the creator page indexes are
    empty, and a full scan of one of them costs the planner as little as the correct walk."""
    viewer = f"inttest-{uuid4()}"
    await _seed(async_session, [
        _row(viewer, _BASE - timedelta(seconds=i), status=(S.READY, S.FAILED, S.RELEASED)[i % 3],
             rtype=("VM", "STATIC_VM")[i % 2], label="needle" if i % 97 == 0 else "other")
        for i in range(2000)
    ])
    await async_session.execute(text("ANALYZE bookings"))
    for stmt, bound in (
        (_label_page_keys_stmt(viewer, resource_types=_VM_TYPES, label="needle",
                               include_released=include_released, limit=_LABEL_LIMIT,
                               scan_size=_LABEL_SCAN, after=None), _LABEL_BOUND),
        (_page_keys_stmt(viewer, resource_types=_VM_TYPES, include_released=include_released,
                         limit=_LIMIT, after=None), _BOUND),
    ):
        _assert_walks_use_own_index_conditions(
            await _page_plan(async_session, stmt), include_released=include_released, after=None,
            bound=bound, types=_VM_TYPES,
        )


async def test_pinned_label_query_is_not_jit_compiled(async_session):
    """With sorts disabled, the label window's own small sorts carry the planner's disable
    penalty, which lifts the estimated cost past jit_above_cost; the pin turns JIT off so a
    millisecond query isn't compiled for most of a second."""
    owner = f"inttest-{uuid4()}"
    await _seed(async_session, _spaced(owner, 30, label="other"))
    await async_session.execute(text("SET LOCAL jit = on"))
    await async_session.execute(text("SET LOCAL jit_above_cost = 100000"))
    stmt = _label_page_keys_stmt(owner, resource_types=_VM_TYPES, label="needle", include_released=True,
                                 limit=3, scan_size=10, after=None)
    async with _OrderedWalk(async_session):
        [plan] = (await async_session.execute(
            text(f"EXPLAIN (ANALYZE, FORMAT JSON) {_sql(stmt)}"))).scalars()
    plan = (json.loads(plan) if isinstance(plan, str) else plan)[0]
    assert plan["Plan"]["Total Cost"] > 100000   # the penalty is there…
    assert "JIT" not in plan                     # …but nothing is compiled
    assert (await async_session.execute(text("SELECT current_setting('jit')"))).scalar_one() == "on"
