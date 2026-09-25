"""Integration: keyset pagination of the environments list (#467).

`list_page` returns at most `limit` environments ordered (created_at DESC, id DESC), continues
strictly after a (created_at, id) cursor, and loads children for the page's environments only.
Every traversal is checked against the unpaginated list with the same filters, so pagination
can't drop, repeat or reorder rows. Guarantee scope (design.md, Decision 8): the environment
index read is bounded by the page size only for the unfiltered list; selective filters are
checked for correctness and per-page child loading, not for a row-count bound.
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
from app.infrastructure.database.models import BookingModel, EnvironmentModel
from app.infrastructure.repositories.environment_repo import (
    EnvironmentRepository,
    _page_stmt,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

S = BookingStatus
_repo = EnvironmentRepository()
_INDEX = "ix_environments_created_at_id"
# Far in the future so seeded rows sort before any other environment in the test database.
_BASE = datetime(2999, 1, 1, tzinfo=timezone.utc)


async def _seed(
    session: AsyncSession, owner: str, specs: list[tuple[datetime, list[BookingStatus]]],
    *, name: str = "env",
) -> list[UUID]:
    """Insert one environment per (created_at, child statuses) spec; return the env ids."""
    env_ids, env_rows, child_rows = [], [], []
    for i, (created_at, statuses) in enumerate(specs):
        env_id = uuid4()
        env_ids.append(env_id)
        env_rows.append({
            "id": env_id, "name": f"{name}-{i}", "user_id": owner, "ttl_minutes": 60,
            "expires_at": PERMANENT_EXPIRES_AT, "construction_complete": True,
            "created_at": created_at,
        })
        child_rows += [{
            "id": uuid4(), "user_id": owner, "status": s.value, "resource_type": "VM",
            "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT, "environment_id": env_id,
        } for s in statuses]
    await session.execute(insert(EnvironmentModel), env_rows)
    if child_rows:
        await session.execute(insert(BookingModel), child_rows)
    await session.flush()
    return env_ids


def _spaced(n: int, statuses=(S.READY,), step=timedelta(seconds=1)):
    """n specs, newest first, `step` apart."""
    return [(_BASE - i * step, list(statuses)) for i in range(n)]


async def _traverse(session, *, limit, user_id, label=None, include_released=True):
    """Follow next_cursor from the first page to the last; return the pages' id lists."""
    pages, after = [], None
    while True:
        page = await _repo.list_page(
            session, user_id=user_id, label=label, include_released=include_released,
            limit=limit, after=after,
        )
        assert len(page.items) <= limit
        pages.append([e.id for e in page.items])
        if page.next_cursor is None:
            return pages
        # The cursor is the last row shown on this page.
        assert page.next_cursor == KeysetCursor(page.items[-1].created_at, page.items[-1].id)
        after = page.next_cursor


async def _unpaginated(session, *, user_id, label=None, include_released=True) -> list[UUID]:
    envs = await _repo._list(session, user_id, label=label, include_released=include_released)
    return [e.id for e in envs]


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
    ids = await _seed(async_session, owner, _spaced(n))

    page = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=5, after=None,
    )
    assert [e.id for e in page.items] == ids[:first_page]   # newest first
    assert (page.next_cursor is not None) is has_more


async def test_boundary_inside_equal_created_at_orders_by_id_desc(async_session):
    owner = f"inttest-{uuid4()}"
    # 2 newer rows, 9 sharing one timestamp (spans pages at limit=4), 2 older rows.
    tie = _BASE - timedelta(seconds=10)
    newer = await _seed(async_session, owner, _spaced(2))
    tied = await _seed(async_session, owner, [(tie, [S.READY])] * 9)
    older = await _seed(async_session, owner, [(tie - timedelta(seconds=1), [S.READY]),
                                               (tie - timedelta(seconds=2), [S.READY])])

    pages = await _traverse(async_session, limit=4, user_id=owner)

    expected = newer + sorted(tied, reverse=True) + older
    assert _flat(pages) == expected
    assert len(set(_flat(pages))) == len(expected)
    assert [len(p) for p in pages] == [4, 4, 4, 1]


async def test_cursor_keeps_microsecond_precision(async_session):
    owner = f"inttest-{uuid4()}"
    a, b = await _seed(async_session, owner, [(_BASE, [S.READY]),
                                              (_BASE - timedelta(microseconds=1), [S.READY])])

    first = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=1, after=None,
    )
    assert [e.id for e in first.items] == [a]
    assert first.next_cursor.created_at == _BASE
    second = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=1,
        after=first.next_cursor,
    )
    assert [e.id for e in second.items] == [b]
    assert second.next_cursor is None


async def test_full_traversal_equals_unpaginated_list(async_session):
    owner = f"inttest-{uuid4()}"
    specs = _spaced(17, step=timedelta(milliseconds=7))
    specs[5] = specs[6] = specs[7] = (specs[5][0], [S.READY])   # a tie mid-stream
    await _seed(async_session, owner, specs)

    for limit in (1, 3, 5, 17, 50):
        pages = await _traverse(async_session, limit=limit, user_id=owner)
        assert _flat(pages) == await _unpaginated(async_session, user_id=owner), limit


async def test_row_added_after_first_page_does_not_shift_later_pages(async_session):
    owner = f"inttest-{uuid4()}"
    ids = await _seed(async_session, owner, _spaced(6))
    first = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=3, after=None,
    )
    await _seed(async_session, owner, [(_BASE + timedelta(seconds=1), [S.READY])])  # newer

    second = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=3,
        after=first.next_cursor,
    )
    assert [e.id for e in second.items] == ids[3:]


# ── 4.2: filters × pagination ────────────────────────────────────────────────────────────────

async def _seed_two_owners(session, token: str):
    """Interleave (in created_at order) two owners' released / unreleased / label-(non)matching
    environments, so every filter's matches straddle page boundaries."""
    owner, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    cycle = [
        (owner, [S.READY], f"{token}-a"),
        (other, [S.READY], f"{token}-b"),
        (owner, [S.RELEASED, S.RELEASED], f"{token}-c"),
        (owner, [S.READY], "unrelated"),
        (other, [S.RELEASED], f"{token}-d"),
        (owner, [], f"{token}-e"),
        (owner, [S.RELEASED, S.READY], "unrelated"),
    ]
    for i in range(21):
        who, statuses, name = cycle[i % len(cycle)]
        await _seed(session, who, [(_BASE - timedelta(seconds=i), statuses)], name=name)
    return owner, other


@pytest.mark.parametrize("mine", [True, False], ids=["mine", "all"])
@pytest.mark.parametrize("use_label", [True, False], ids=["label", "no-label"])
@pytest.mark.parametrize("include_released", [False, True], ids=["hide-released", "show-released"])
async def test_filter_combinations_traverse_exactly_the_filtered_list(
    async_session, mine, use_label, include_released,
):
    token = f"tok{uuid4().hex[:8]}"
    owner, other = await _seed_two_owners(async_session, token)
    user_id = owner if mine else None
    label = token if use_label else None

    expected = await _unpaginated(
        async_session, user_id=user_id, label=label, include_released=include_released,
    )
    pages = await _traverse(
        async_session, limit=3, user_id=user_id, label=label, include_released=include_released,
    )
    assert _flat(pages) == expected
    assert len(set(_flat(pages))) == len(expected)

    seen = await _repo._list(async_session, user_id, label=label, include_released=include_released)
    owners = {e.user_id for e in seen}
    if mine:
        assert owners == {owner}
    else:
        assert other in owners   # All pages continue past the user's own environments
    if not include_released:
        from app.domain.environment_status import derive_environment_status
        assert all(derive_environment_status(b.status for b in e.bookings) != S.RELEASED for e in seen)


# ── 4.3: children per page, query plan, bounded unfiltered read ─────────────────────────────

async def test_children_loaded_only_for_the_current_page(async_session, monkeypatch):
    owner = f"inttest-{uuid4()}"
    ids = await _seed(async_session, owner, _spaced(7, statuses=(S.READY, S.READY)))
    loaded: list[list[UUID]] = []
    original = _repo._children_batch

    async def spy(session, env_ids):
        loaded.append(list(env_ids))
        return await original(session, env_ids)

    monkeypatch.setattr(_repo, "_children_batch", spy)
    first = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=3, after=None,
    )
    second = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=True, limit=3,
        after=first.next_cursor,
    )

    assert loaded == [ids[0:3], ids[3:6]]   # never the lookahead row (ids[3] / ids[6] resp.)
    assert all(len(e.bookings) == 2 for e in first.items + second.items)


async def test_sparse_mine_page_is_correct_and_loads_only_its_children(async_session, monkeypatch):
    """The user's environments are older than many others': the index walk passes those (no
    row-count bound asserted — Decision 8), but the page and its child loading stay exact."""
    owner, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    await _seed(async_session, other, _spaced(60))
    mine = await _seed(async_session, owner, [
        (_BASE - timedelta(minutes=5) - timedelta(seconds=i), [S.READY]) for i in range(4)
    ])
    loaded: list[list[UUID]] = []
    original = _repo._children_batch

    async def spy(session, env_ids):
        loaded.append(list(env_ids))
        return await original(session, env_ids)

    monkeypatch.setattr(_repo, "_children_batch", spy)
    page = await _repo.list_page(
        async_session, user_id=owner, label=None, include_released=False, limit=3, after=None,
    )
    assert [e.id for e in page.items] == mine[:3]
    assert loaded == [mine[:3]]


async def _explain(session, stmt, *, analyze=False) -> str:
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    if analyze:
        [plan] = (await session.execute(text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"))).scalars()
        return plan if isinstance(plan, str) else json.dumps(plan)
    return "\n".join((await session.execute(text(f"EXPLAIN {sql}"))).scalars())


_CURSOR = KeysetCursor(created_at=_BASE - timedelta(seconds=30), id=UUID(int=2**127))


@pytest.mark.parametrize("after", [None, _CURSOR], ids=["first-page", "after-cursor"])
@pytest.mark.parametrize("user_id, label, include_released", [
    pytest.param(None, None, True, id="all"),
    pytest.param(None, None, False, id="all-hide-released"),
    pytest.param("OWNER", None, False, id="mine-hide-released"),
    pytest.param("OWNER", "tok", True, id="mine-label"),
    pytest.param(None, "tok", False, id="all-label-hide-released"),
])
async def test_page_plan_walks_the_index_in_order(async_session, after, user_id, label, include_released):
    owner = f"inttest-{uuid4()}"
    await _seed(async_session, owner, _spaced(120))
    await async_session.execute(text("ANALYZE environments"))
    await async_session.execute(text("ANALYZE bookings"))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))

    stmt = _page_stmt(
        owner if user_id == "OWNER" else None, label=label, include_released=include_released,
        limit=50, after=after,
    )
    plan = await _explain(async_session, stmt)

    assert f"Index Scan Backward using {_INDEX} on environments" in plan, plan
    assert "Sort" not in plan, plan
    if after is not None:
        # The cursor is an index condition: rows before it are never read.
        assert "Index Cond: (ROW(created_at, id) < ROW(" in plan, plan


def _scan_nodes(node: dict):
    if node.get("Index Name") == _INDEX:
        yield node
    for child in node.get("Plans", []):
        yield from _scan_nodes(child)


@pytest.mark.parametrize("from_cursor", [False, True], ids=["first-page", "after-cursor"])
async def test_unfiltered_page_reads_at_most_limit_plus_one_index_entries(async_session, from_cursor):
    limit = 10
    owner = f"inttest-{uuid4()}"
    ids = await _seed(async_session, owner, _spaced(5 * limit))   # well over two pages
    await async_session.execute(text("ANALYZE environments"))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))

    after = None
    if from_cursor:
        first = await _repo.list_page(
            async_session, user_id=None, label=None, include_released=True, limit=limit, after=None,
        )
        assert [e.id for e in first.items] == ids[:limit]
        after = first.next_cursor
    stmt = _page_stmt(None, label=None, include_released=True, limit=limit, after=after)
    [plan] = json.loads(await _explain(async_session, stmt, analyze=True))

    [scan] = list(_scan_nodes(plan["Plan"]))
    assert scan["Actual Rows"] * scan["Actual Loops"] <= limit + 1, scan


@pytest.mark.parametrize("label, include_released", [
    pytest.param(None, True, id="mine-show-released"),
    pytest.param(None, False, id="mine-hide-released"),
    pytest.param("tok", True, id="mine-label"),
])
async def test_planner_choice_does_not_change_the_page(async_session, label, include_released):
    """Selective filters may get a seq scan + top-N sort instead of the index walk (Decision 8);
    either plan must yield the same pages and cursors."""
    owner, other = f"inttest-{uuid4()}", f"inttest-{uuid4()}"
    await _seed(async_session, other, _spaced(300, step=timedelta(milliseconds=3)))
    specs = [(_BASE - timedelta(seconds=2 * i, microseconds=i), [S.RELEASED] if i % 3 == 0 else [S.READY])
             for i in range(12)]
    specs[4] = specs[5] = (specs[4][0], [S.READY])   # a tie
    await _seed(async_session, owner, specs, name="tok")
    await async_session.execute(text("ANALYZE environments"))
    await async_session.execute(text("ANALYZE bookings"))

    free = await _traverse(async_session, limit=4, user_id=owner, label=label,
                           include_released=include_released)
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))
    forced = await _traverse(async_session, limit=4, user_id=owner, label=label,
                             include_released=include_released)
    assert free == forced
    assert _flat(free) == await _unpaginated(
        async_session, user_id=owner, label=label, include_released=include_released,
    )
