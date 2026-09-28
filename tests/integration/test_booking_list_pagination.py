"""Integration: keyset pagination of the browser bookings lists (#479).

`list_page` returns at most `limit` bookings ordered (created_at DESC, id DESC) and continues
strictly after a (created_at, id) cursor. Every traversal is checked against the unpaginated
`list_all` / `list_by_user` with the same filters, so pagination can't drop, repeat or reorder rows.

Guarantee (design.md, Decisions 2, 3, 9 and 10): without a label, page selection reads at most
4 × (limit + 1) booking index entries on the plan the page query runs — pinned by `list_page` to
the ordered index walks; plans here are taken under that same pin, never a setting of the test's
own — however much RELEASED / FAILED history other users or other resource types have. The label filter is the one
exception (#485). Queue rank reads only QUEUED rows.
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
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _OrderedWalk,
    _page_keys_stmt,
    _queue_rank_stmt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_repo = BookingRepository()
_VM_TYPES = ["VM", "STATIC_VM"]
_NS_TYPES = ["NAMESPACE"]
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
                    include_released=True):
    """Follow next_cursor from the first page to the last; return the pages' id lists."""
    pages, after = [], None
    while True:
        page = await _repo.list_page(
            session, user_id=user_id, resource_types=resource_types, label=label,
            include_released=include_released, limit=limit, after=after,
        )
        assert len(page.items) <= limit
        pages.append([b.id for b in page.items])
        if page.next_cursor is None:
            return pages
        # The cursor is the last row shown on this page.
        assert page.next_cursor == KeysetCursor(page.items[-1].created_at, page.items[-1].id)
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
        include_released=True, limit=5, after=None,
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
        include_released=True, limit=1, after=None,
    )
    assert [x.id for x in first.items] == [a]
    assert first.next_cursor.created_at == _BASE
    second = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=1, after=first.next_cursor,
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
        include_released=True, limit=3, after=None,
    )
    await _seed(async_session, [_row(owner, _BASE + timedelta(seconds=1))])   # newer
    second = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=3, after=first.next_cursor,
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


def _assert_bounded(plan: dict, *, include_released: bool, mine: bool, after) -> None:
    nodes = list(_nodes(plan))
    scans = [n for n in nodes if n.get("Relation Name") == "bookings"]
    assert scans, plan
    assert all(n["Node Type"] in ("Index Scan", "Index Only Scan") for n in scans), plan
    assert not [n for n in nodes if n["Node Type"].startswith("Bitmap")], plan
    read = sum(n["Actual Rows"] * n["Actual Loops"] for n in scans)
    assert read <= _BOUND, (read, plan)
    for n in nodes:
        if n["Node Type"] == "Sort":
            assert all(c["Actual Rows"] <= _BOUND for c in n.get("Plans", [])), plan
    for scan in scans:
        assert scan["Index Name"].endswith("_unreleased") is (not include_released), scan
        assert scan["Index Name"].startswith(
            ("ix_bookings_owner_page", "ix_bookings_creator_page") if mine else ("ix_bookings_type_page",)
        ), scan
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
        include_released=include_released, limit=_LIMIT, after=None,
    )
    assert len(first.items) == _LIMIT and first.next_cursor is not None
    if not include_released:
        assert any(b.status == S.FAILED for b in first.items) or not mine

    for after in (None, first.next_cursor):
        stmt = _page_keys_stmt(
            user_id, resource_types=resource_types, label=None,
            include_released=include_released, limit=_LIMIT, after=after,
        )
        plan = await _page_plan(async_session, stmt)
        _assert_bounded(plan, include_released=include_released, mine=mine, after=after)


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
    stmt = _page_keys_stmt(viewer, resource_types=_NS_TYPES, label=None,
                           include_released=include_released, limit=_LIMIT, after=None)
    plan = await _page_plan(async_session, stmt)
    _assert_bounded(plan, include_released=include_released, mine=True, after=None)


async def test_list_page_restores_the_previous_planner_settings(async_session):
    """The pin is scoped to the key query: afterwards the request plans as before — including an
    operator's non-default value, which is restored rather than reset to the default."""
    owner = f"inttest-{uuid4()}"
    await _seed(async_session, _spaced(owner, 3))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))
    await async_session.execute(text("SET LOCAL enable_bitmapscan = on"))

    page = await _repo.list_page(
        async_session, user_id=owner, resource_types=_VM_TYPES, label=None,
        include_released=True, limit=2, after=None,
    )

    assert len(page.items) == 2
    settings = (await async_session.execute(text(
        "SELECT current_setting('enable_bitmapscan'), current_setting('enable_seqscan')"
    ))).one()
    assert tuple(settings) == ("on", "off")


async def test_partial_index_is_used_under_a_generic_plan(async_session):
    """The released predicate is a literal, so even a generic prepared plan (bound owner and type,
    as asyncpg sends them) can use the RELEASED-free index (design.md, Decision 2)."""
    data = await _seed_history(async_session)
    stmt = _page_keys_stmt(
        data["viewer"], resource_types=["VM"], label=None, include_released=False,
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


# ── 4.4: queue rank, and the label exception ─────────────────────────────────────────────────

async def test_queue_rank_reads_only_the_queue(async_session):
    await _seed_history(async_session)
    owner = f"inttest-{uuid4()}"
    queued = await _seed(async_session, [
        _row(owner, _BASE - timedelta(days=1, seconds=i), status=S.QUEUED, rtype="NAMESPACE")
        for i in range(5)
    ])
    await async_session.execute(text("ANALYZE bookings"))
    stmt = _queue_rank_stmt("NAMESPACE", _BASE)
    plan = await _explain_analyze(async_session, _sql(stmt))
    scans = [n for n in _nodes(plan) if n.get("Relation Name") == "bookings"]
    assert [n["Index Name"] for n in scans] == ["ix_bookings_queued_rank"], plan
    total_queued = (await async_session.execute(text(
        "SELECT count(*) FROM bookings WHERE resource_type = 'NAMESPACE' AND status = 'QUEUED'"
    ))).scalar_one()
    assert total_queued >= len(queued)
    assert sum(n["Actual Rows"] * n["Actual Loops"] for n in scans) <= total_queued, plan


async def test_label_walk_stays_within_the_users_own_branches(async_session):
    """The label filter is the one unbounded case (#485), but a Mine walk never leaves the
    viewer's owner / creator index ranges, however common the label is among other users."""
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
        include_released=True, limit=_LIMIT, after=None,
    )
    assert [b.id for b in page.items] == [r["id"] for r in rows if r["label"] == f"{token}-mine"]

    stmt = _page_keys_stmt(owner, resource_types=_VM_TYPES, label=token, include_released=True,
                           limit=_LIMIT, after=None)
    plan = await _page_plan(async_session, stmt)
    scans = [n for n in _nodes(plan) if n.get("Relation Name") == "bookings"]
    assert scans and all(
        n["Node Type"] in ("Index Scan", "Index Only Scan")
        and n["Index Name"].startswith(("ix_bookings_owner_page", "ix_bookings_creator_page"))
        for n in scans
    ), plan
