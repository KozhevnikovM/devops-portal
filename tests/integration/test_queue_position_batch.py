"""Integration: batched queue-position reads (#495) on real PostgreSQL.

A bookings page reads the FIFO positions of all its QUEUED rows in one statement; the rank is
``1 + count(QUEUED of the type created strictly earlier)`` (ties share a position). The cost probes
print round trips, rows examined and shared buffers for the old per-row COUNT and the new bulk
read (run with ``-s``) so they can be recorded with the change; only relative/structural facts are
asserted — never a wall-clock threshold.
"""
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, insert, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus
from app.infrastructure.auth import require_user
from app.infrastructure.database.models import BookingModel, UserModel
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories._ordered_walk import _OrderedWalk
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _queue_rank_stmt,
)
from app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# Far in the future so the seeded rows are the newest in the test database (the pages list them
# first); every test runs in a rolled-back transaction, so nothing leaks between tests.
_BASE = datetime(2999, 9, 1, tzinfo=timezone.utc)

# Dataset (a): a large active queue. Dataset (b): a short queue over a large history.
_DATASETS = {
    "a_large_queue": {"queued_ns": 5000, "queued_svm": 200, "history": 0},
    "b_large_history": {"queued_ns": 60, "queued_svm": 60, "history": 20000},
}

# The pre-#495 per-row rank statement, kept verbatim as the "old" side of the comparison.
_OLD_RANK_SQL = (
    "SELECT count(bookings.id) FROM bookings WHERE bookings.resource_type = :t "
    "AND status = 'QUEUED' AND bookings.created_at < :c"
)


def _domain_user(row: UserModel) -> User:
    return User(id=row.id, username=row.username, password_hash="", role=row.role,
                is_active=True, created_at=_BASE)


async def _seed(session, *, queued_ns: int, queued_svm: int, history: int) -> dict:
    """Seed one dataset, newest first: queued namespaces and static VMs interleaved with READY
    VMs, then `history` RELEASED/FAILED namespace bookings older than every queued one."""
    owner = UserModel(id=uuid4(), username=f"q{uuid4().hex[:8]}-owner", password_hash="x",
                      role="user")
    admin = UserModel(id=uuid4(), username=f"q{uuid4().hex[:8]}-admin", password_hash="x",
                      role="admin")
    session.add_all([owner, admin])
    await session.flush()

    common = {"user_id": str(owner.id), "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT}
    rows: list[dict] = []
    ns_ids: list = []   # oldest → newest
    svm_ids: list = []
    for i in range(queued_ns):
        ns_ids.append(uuid4())
        rows.append({**common, "id": ns_ids[-1], "resource_type": "NAMESPACE",
                     "status": "QUEUED", "created_at": _BASE - timedelta(seconds=queued_ns - i)})
    for i in range(queued_svm):
        svm_ids.append(uuid4())
        created = _BASE - timedelta(seconds=queued_svm - i, milliseconds=500)
        rows.append({**common, "id": svm_ids[-1], "resource_type": "STATIC_VM",
                     "status": "QUEUED", "created_at": created})
        if i % 4 == 0:  # some non-queued rows on the VM page
            rows.append({**common, "id": uuid4(), "resource_type": "VM", "status": "READY",
                         "created_at": created + timedelta(milliseconds=100)})
    oldest = _BASE - timedelta(seconds=max(queued_ns, queued_svm) + 1)
    for i in range(history):
        rows.append({**common, "id": uuid4(), "resource_type": "NAMESPACE",
                     "status": "RELEASED" if i % 3 else "FAILED",
                     "created_at": oldest - timedelta(seconds=i)})
    for start in range(0, len(rows), 5000):
        await session.execute(insert(BookingModel), rows[start:start + 5000])
    await session.flush()
    await session.execute(text("ANALYZE bookings"))
    return {"owner": _domain_user(owner), "admin": _domain_user(admin),
            "ns_ids": ns_ids, "svm_ids": svm_ids}


@pytest_asyncio.fixture(loop_scope="session", params=list(_DATASETS))
async def dataset(request, async_engine, async_session):
    # Dead index entries left by earlier tests' rolled-back rows share this key range and would
    # inflate both the old and the new cost; clear them so the numbers compare like with like.
    async with async_engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("VACUUM ANALYZE bookings"))
    seeded = await _seed(async_session, **_DATASETS[request.param])
    return {"name": request.param, **seeded}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _is_rank_statement(sql: str) -> bool:
    """A queue-position read: old per-row COUNT or the new bulk rank()."""
    return "QUEUED" in sql and ("rank()" in sql or "count(bookings.id)" in sql)


async def _get(session, user: User, url: str) -> dict:
    """One GET as ``user``: response text, total statements and queue-position statements."""
    async def _session():
        yield session

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[require_user] = lambda: user
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = session.bind.engine.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert response.status_code == 200, response.text[:500]
    return {"text": response.text, "statements": len(statements),
            "rank_statements": sum(_is_rank_statement(s) for s in statements)}


def _plan_cost(plan_json) -> dict:
    """Rows examined on `bookings` and shared buffers touched, from an EXPLAIN (ANALYZE, BUFFERS,
    FORMAT JSON) result."""
    top = plan_json[0]["Plan"]
    rows = 0

    def walk(node):
        nonlocal rows
        if node.get("Relation Name") == "bookings":
            rows += node["Actual Rows"] * node["Actual Loops"] + node.get("Rows Removed by Filter", 0)
        for child in node.get("Plans", []):
            walk(child)

    walk(top)
    return {"rows": rows,
            "buffers": top.get("Shared Hit Blocks", 0) + top.get("Shared Read Blocks", 0)}


async def _explain(session, sql: str, params: dict) -> dict:
    result = await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params)
    return result.scalar_one()


async def _old_cost(session, resource_type: str, booking_ids) -> dict:
    """Summed cost of the pre-#495 path: one COUNT round trip per queued row."""
    created = dict((await session.execute(
        text("SELECT id, created_at FROM bookings WHERE id = ANY(:ids)"), {"ids": list(booking_ids)},
    )).all())
    total = {"round_trips": 0, "rows": 0, "buffers": 0}
    for booking_id in booking_ids:
        cost = _plan_cost(await _explain(session, _OLD_RANK_SQL,
                                         {"t": resource_type, "c": created[booking_id]}))
        total["round_trips"] += 1
        total["rows"] += cost["rows"]
        total["buffers"] += cost["buffers"]
    return total


async def test_baseline_page_cost(async_session, dataset):
    """Print the page's queue-position cost: statements per page and the per-row COUNT plans."""
    page = await _get(async_session, dataset["admin"], "/book/namespace?filter=all")
    visible = dataset["ns_ids"][-50:]  # the page's rows: the 50 newest, all QUEUED
    old = await _old_cost(async_session, "NAMESPACE", visible)
    print(f"\n[#495 cost] {dataset['name']:<16} namespace page: statements={page['statements']} "
          f"rank_statements={page['rank_statements']} old_per_row_counts={old}")
    assert "Queued — position" in page["text"]


# ── Rank semantics (repository) ─────────────────────────────────────────────────

# Earlier than anything else in the database, so absolute positions are deterministic.
_EPOCH = datetime(1001, 1, 1, tzinfo=timezone.utc)
_repo = BookingRepository()


async def _add(session, resource_type: str, seconds: float, status: str = "QUEUED",
               user_id: str = "owner-1", created_by: str | None = None) -> Booking:
    """Insert one booking created `seconds` after _EPOCH; return its domain entity."""
    booking_id = uuid4()
    await session.execute(insert(BookingModel), [{
        "id": booking_id, "user_id": user_id, "created_by": created_by,
        "resource_type": resource_type, "status": status, "ttl_minutes": 60,
        "expires_at": PERMANENT_EXPIRES_AT, "created_at": _EPOCH + timedelta(seconds=seconds),
    }])
    await session.flush()
    return await _repo.get(session, booking_id)


async def test_each_type_is_ranked_within_its_own_queue(async_session):
    ns = [await _add(async_session, "NAMESPACE", s) for s in (1, 3, 5)]
    svm = [await _add(async_session, "STATIC_VM", s) for s in (2, 4)]
    vm = [await _add(async_session, "VM", s) for s in (0, 6)]
    positions = await _repo.queue_positions(async_session, ns + svm + vm)
    assert positions == {
        ns[0].id: 1, ns[1].id: 2, ns[2].id: 3, svm[0].id: 1, svm[1].id: 2, vm[0].id: 1, vm[1].id: 2,
    }


async def test_sparse_subset_returns_only_the_requested_ids(async_session):
    queue = [await _add(async_session, "NAMESPACE", s) for s in range(1, 46)]
    positions = await _repo.queue_positions(async_session, [queue[1], queue[39]])
    assert positions == {queue[1].id: 2, queue[39].id: 40}


async def test_tied_created_at_share_a_position(async_session):
    first = await _add(async_session, "STATIC_VM", 1)
    tied = [await _add(async_session, "STATIC_VM", 2) for _ in range(2)]
    later = await _add(async_session, "STATIC_VM", 3)
    positions = await _repo.queue_positions(async_session, [first, *tied, later])
    assert positions == {first.id: 1, tied[0].id: 2, tied[1].id: 2, later.id: 4}


async def test_non_queued_bookings_get_no_position(async_session):
    queued = await _add(async_session, "NAMESPACE", 2)
    others = [await _add(async_session, "NAMESPACE", 1, status=s)
              for s in ("READY", "FAILED", "RELEASED")]
    positions = await _repo.queue_positions(async_session, [queued, *others])
    # Non-queued bookings are neither ranked nor counted ahead of the queued one.
    assert positions == {queued.id: 1}


async def test_queue_is_global_across_owners_and_creators(async_session):
    other = await _add(async_session, "NAMESPACE", 1, user_id="owner-2")
    on_behalf = await _add(async_session, "NAMESPACE", 2, user_id="owner-3", created_by="disp-1")
    mine = await _add(async_session, "NAMESPACE", 3, user_id="owner-1")
    assert await _repo.queue_positions(async_session, [mine]) == {mine.id: 3}
    assert await _repo.queue_positions(async_session, [on_behalf, mine]) == {
        on_behalf.id: 2, mine.id: 3,
    }
    assert other.id not in await _repo.queue_positions(async_session, [mine])


async def test_matches_the_count_rule_on_a_random_queue_with_ties(async_session):
    rng = random.Random(495)
    bookings = [
        await _add(async_session, rng.choice(["NAMESPACE", "STATIC_VM"]), rng.randrange(40),
                   status=rng.choice(["QUEUED", "QUEUED", "QUEUED", "READY", "RELEASED"]))
        for _ in range(150)
    ]
    requested = rng.sample(bookings, 40)
    positions = await _repo.queue_positions(async_session, requested)

    expected = {}
    for b in requested:
        if b.status == BookingStatus.QUEUED:
            ahead = (await async_session.execute(
                text(_OLD_RANK_SQL), {"t": b.resource_type.value, "c": b.created_at},
            )).scalar_one()
            expected[b.id] = ahead + 1
    assert positions == expected


async def test_no_statement_for_an_empty_request(async_session):
    statements = []
    engine = async_session.bind.engine.sync_engine
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        assert await _repo.queue_positions(async_session, []) == {}
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert statements == []


async def test_positions_come_from_one_view_after_a_concurrent_promotion(async_engine):
    """A booking promoted (committed elsewhere) after the list read but before the rank read has
    no position; the others are ranked against the queue as committed, by one statement."""
    async with AsyncSession(async_engine, expire_on_commit=False) as setup:
        listed = [await _add(setup, "NAMESPACE", s, user_id="snapshot-test") for s in (1, 2, 3)]
        await setup.commit()
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as reader:
            # The list read saw all three QUEUED; now the head is promoted by another session.
            async with AsyncSession(async_engine) as other:
                await other.execute(update(BookingModel).where(BookingModel.id == listed[0].id)
                                    .values(status="READY"))
                await other.commit()
            statements = []
            engine = async_engine.sync_engine
            listener = lambda *args: statements.append(args[2])
            event.listen(engine, "before_cursor_execute", listener)
            try:
                positions = await _repo.queue_positions(reader, listed)
            finally:
                event.remove(engine, "before_cursor_execute", listener)
        assert positions == {listed[1].id: 1, listed[2].id: 2}
        assert sum(_is_rank_statement(s) for s in statements) == 1
    finally:
        async with AsyncSession(async_engine) as cleanup:
            await cleanup.execute(delete(BookingModel).where(BookingModel.user_id == "snapshot-test"))
            await cleanup.commit()


# ── Statement count per page surface ───────────────────────────────────────────

def _load_more_url(html: str) -> str:
    anchor = html.index('id="bookings-load-more"')
    start = html.index('hx-get="', anchor) + len('hx-get="')
    return html[start:html.index('"', start)].replace("&amp;", "&")


@pytest_asyncio.fixture(loop_scope="session")
async def small_queue(async_session):
    """More than a page of queued namespaces and static VMs (plus READY VMs) — no history."""
    return await _seed(async_session, queued_ns=60, queued_svm=60, history=0)


@pytest.mark.parametrize("path", ["/book/namespace", "/book/vm"])
async def test_one_rank_statement_per_page_surface(async_session, small_queue, path):
    """The page, the filter-change fragment and Load more each read every queued row's position
    with exactly one statement."""
    admin = small_queue["admin"]
    page = await _get(async_session, admin, f"{path}?filter=all")
    fragment = await _get(async_session, admin, f"{path}/list?filter=all")
    more = await _get(async_session, admin, _load_more_url(page["text"]))
    for name, response in (("page", page), ("list", fragment), ("rows", more)):
        assert response["text"].count("Queued — position") >= 2, name
        assert "Queued — position —" not in response["text"], name
        assert response["rank_statements"] == 1, name


async def test_no_rank_statement_without_queued_rows(async_session, small_queue):
    solo = UserModel(id=uuid4(), username=f"q{uuid4().hex[:8]}-solo", password_hash="x", role="user")
    async_session.add(solo)
    await async_session.flush()
    await async_session.execute(insert(BookingModel), [
        {"id": uuid4(), "user_id": str(solo.id), "resource_type": "VM", "status": status,
         "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT,
         "created_at": _BASE + timedelta(seconds=i)}
        for i, status in enumerate(["READY", "FAILED", "PROVISIONING"])
    ])
    user = _domain_user(solo)
    for url in ("/book/vm", "/book/vm/list?filter=mine"):
        response = await _get(async_session, user, url)
        assert response["text"].count('id="booking-') >= 3, url
        assert response["rank_statements"] == 0, url


# ── Plan shape and cost: old per-row COUNT vs one bulk read ─────────────────────

_PINNED = ("enable_bitmapscan", "enable_seqscan", "enable_sort", "enable_indexscan", "jit")


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


def _bulk_sql(visible: list[Booking]) -> str:
    """The repository's bulk rank statement for `visible`, with literal parameters."""
    types = {b.resource_type.value for b in visible}
    stmt = _queue_rank_stmt(
        {t: max(b.created_at for b in visible if b.resource_type.value == t) for t in types},
        [b.id for b in visible],
    )
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _new_plan(session, visible: list[Booking]):
    """EXPLAIN the bulk statement under the same plan pin the repository runs it with."""
    async with _OrderedWalk(session):
        return await _explain(session, _bulk_sql(visible), {})


async def _settings(session) -> dict:
    return {name: (await session.execute(text(f"SELECT current_setting('{name}')"))).scalar_one()
            for name in _PINNED}


@pytest.mark.parametrize("resource_type", ["NAMESPACE", "STATIC_VM"])
async def test_bulk_read_walks_each_queue_entry_once_and_never_history(async_session, dataset,
                                                                      resource_type):
    ids = dataset["ns_ids"] if resource_type == "NAMESPACE" else dataset["svm_ids"]
    visible = [await _repo.get(async_session, booking_id) for booking_id in ids[-50:]]
    plan = await _new_plan(async_session, visible)
    new = {"round_trips": 1, **_plan_cost(plan)}
    old = await _old_cost(async_session, resource_type, ids[-50:])
    print(f"\n[#495 cost] {dataset['name']:<16} {resource_type:<9} 50 queued rows: "
          f"old={old} new={new}")

    scans = [n for n in _plan_nodes(plan[0]["Plan"]) if n.get("Relation Name") == "bookings"]
    assert len(scans) == 1
    assert scans[0]["Node Type"] == "Index Scan"
    assert scans[0]["Index Name"] == "ix_bookings_queued_rank"
    # Every entry up to the newest visible row of the type, each once — not once per row — and
    # nothing else: no history, no other type.
    assert scans[0]["Actual Rows"] * scans[0]["Actual Loops"] == len(ids)
    assert scans[0].get("Rows Removed by Filter", 0) == 0
    assert new["rows"] < old["rows"]


async def test_bulk_plan_is_one_ordered_walk_per_type_without_seq_scan_or_sort(async_session,
                                                                               dataset):
    visible = [await _repo.get(async_session, booking_id)
               for booking_id in dataset["svm_ids"][-25:] + dataset["ns_ids"][-25:]]
    nodes = list(_plan_nodes((await _new_plan(async_session, visible))[0]["Plan"]))
    node_types = [n["Node Type"] for n in nodes]
    scans = [n for n in nodes if n.get("Relation Name") == "bookings"]
    assert [n["Index Name"] for n in scans] == ["ix_bookings_queued_rank"] * 2
    assert "Seq Scan" not in node_types and "Bitmap Heap Scan" not in node_types
    assert "Sort" not in node_types and "Incremental Sort" not in node_types
    assert node_types.count("WindowAgg") == 2


@pytest.mark.parametrize("previous", [
    {},  # server defaults
    {"enable_bitmapscan": "off", "enable_seqscan": "off", "enable_sort": "on",
     "enable_indexscan": "off", "jit": "on"},  # an operator's non-default values
])
async def test_rank_read_restores_the_previous_planner_settings(async_session, previous):
    """The pin lasts for the rank read alone: the next read plans with the settings as before."""
    for name, value in previous.items():
        await async_session.execute(text(f"SET LOCAL {name} = {value}"))
    before = await _settings(async_session)
    queued = await _add(async_session, "NAMESPACE", 1)
    assert await _repo.queue_positions(async_session, [queued]) == {queued.id: 1}
    assert await _settings(async_session) == before
