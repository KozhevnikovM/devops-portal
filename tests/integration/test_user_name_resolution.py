"""Integration: owner/creator names resolve through the users primary key (#510, openspec change
username-join-pkey-lookup).

- 4.1 every stored reference resolves to the same name as the old `CAST(users.id AS VARCHAR)`
  join, across every read that shows a name — canonical, legacy `dev-user`, a deleted user's id,
  uppercase, braced, non-ASCII lookalikes, a trailing newline and a NULL creator — and no read
  fails; the guard ignores the reference's own collation.
- 4.2 the per-row users_pkey lookup is *available* (sequential scans off), in custom and generic
  plans, reading at most one user per non-NULL reference occurrence.
- 4.3 measured regression, not a spec guarantee: with 20,000 analysed users and default settings
  the planner itself chooses that lookup on the CI baseline (PostgreSQL 15).
- 4.4 the username filters resolve the username once, through users_username_key.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import String, cast, delete, insert, select, text
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.infrastructure.database.models import (
    BookingModel,
    EnvironmentModel,
    NamespaceModel,
    StaticVMModel,
    UserModel,
)
from app.infrastructure.repositories._user_ref import user_ref_uuid
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _list_items_by_ids_stmt,
)
from app.infrastructure.repositories.environment_repo import (
    EnvironmentRepository,
    _with_usernames,
)
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_bookings = BookingRepository()
_environments = EnvironmentRepository()
_namespaces = NamespaceRepository()
_static_vms = StaticVMRepository()
_PLAN_MODES = ["force_custom_plan", "force_generic_plan"]
_ALL_TYPES = ["VM", "STATIC_VM", "NAMESPACE"]
# After every other test's rows (they use year 2999 or earlier), so these are the newest.
_BASE = datetime(3001, 1, 1, tzinfo=timezone.utc)


def _booking(
    user_id: str, created_by: str | None, created_at: datetime, **kw
) -> BookingModel:
    return BookingModel(
        id=uuid4(),
        user_id=user_id,
        created_by=created_by,
        status="READY",
        ttl_minutes=60,
        expires_at=PERMANENT_EXPIRES_AT,
        created_at=created_at,
        **kw,
    )


async def _old_join_names(session: AsyncSession, refs: list[str | None]) -> dict:
    """What the pre-#510 `CAST(users.id AS VARCHAR) = ref` join resolved each reference to."""
    names = {}
    for ref in refs:
        names[ref] = (
            None
            if ref is None
            else (
                await session.execute(
                    select(UserModel.username).where(cast(UserModel.id, String) == ref)
                )
            ).scalar_one_or_none()
        )
    return names


# ── 4.1 name equality ─────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def references(async_session: AsyncSession):
    """One booking, environment (with a namespace child), namespace and static VM per reference
    kind, each owned and dispatched by that reference, plus a NULL-creator booking."""
    tag = uuid4().hex[:8]
    alive = UserModel(
        id=uuid4(), username=f"r510-alive-{tag}", password_hash="x", role="user"
    )
    gone = UserModel(
        id=uuid4(), username=f"r510-gone-{tag}", password_hash="x", role="user"
    )
    async_session.add_all([alive, gone])
    await async_session.flush()
    await async_session.delete(gone)
    await async_session.flush()
    canonical = str(alive.id)
    refs = {
        "canonical": canonical,
        "legacy": "dev-user",
        "deleted": str(gone.id),
        "uppercase": canonical.upper(),
        "braced": "{" + canonical + "}",
        "non_ascii_digit": canonical[:-1] + "٣",
        "non_ascii_letter": canonical[:-1] + "ä",
        "trailing_newline": canonical + "\n",
    }
    rows: dict[str, dict] = {
        "bookings": {},
        "environments": {},
        "namespaces": {},
        "static_vms": {},
    }
    for i, (kind, ref) in enumerate(refs.items()):
        at = _BASE + timedelta(minutes=i)
        booking = _booking(ref, ref, at)
        ns = NamespaceModel(
            id=uuid4(), name=f"r510-{tag}-{i}", cluster_name=f"r510-{tag}"
        )
        svm = StaticVMModel(
            id=uuid4(), name=f"r510-{tag}-{i}", host="h", username="u", password="p"
        )
        env = EnvironmentModel(
            id=uuid4(),
            name=f"r510-{kind}",
            user_id=ref,
            created_by=ref,
            ttl_minutes=60,
            expires_at=PERMANENT_EXPIRES_AT,
            construction_complete=True,
            created_at=at,
        )
        async_session.add_all([booking, ns, svm, env])
        await async_session.flush()
        async_session.add_all(
            [
                _booking(
                    ref,
                    ref,
                    at,
                    resource_type="NAMESPACE",
                    namespace_id=ns.id,
                    environment_id=env.id,
                ),
                _booking(ref, ref, at, resource_type="STATIC_VM", static_vm_id=svm.id),
            ]
        )
        rows["bookings"][kind], rows["environments"][kind] = booking.id, env.id
        rows["namespaces"][kind], rows["static_vms"][kind] = ns, svm.id
    no_creator = _booking(canonical, None, _BASE + timedelta(hours=1))
    async_session.add(no_creator)
    await async_session.flush()
    expected = await _old_join_names(async_session, list(refs.values()))
    return {
        "refs": refs,
        "rows": rows,
        "expected": expected,
        "alive": alive,
        "no_creator": no_creator.id,
    }


async def test_every_reference_resolves_as_the_old_join(async_session, references):
    refs, rows, expected = (
        references["refs"],
        references["rows"],
        references["expected"],
    )
    # The spec's truth table: only the canonical id names a user.
    assert expected == {
        ref: (references["alive"].username if k == "canonical" else None)
        for k, ref in refs.items()
    }

    page = await _bookings.list_page(
        async_session,
        user_id=None,
        resource_types=_ALL_TYPES,
        label=None,
        include_released=True,
        limit=100,
        scan_size=500,
        after=None,
    )
    listed = {item.id: item for item in page.items}
    env_page = await _environments.list_page(
        async_session,
        user_id=None,
        label=None,
        include_released=True,
        limit=100,
        after=None,
    )
    env_listed = {env.id: env for env in env_page.items}
    ns_held = await _namespaces.held_by(async_session)
    svm_held = await _static_vms.held_by(async_session)

    for kind, ref in refs.items():
        name = expected[ref]
        item = listed[rows["bookings"][kind]]
        assert (item.owner_username, item.created_by_username) == (name, name), kind
        booking = await _bookings.get(async_session, rows["bookings"][kind])
        assert (booking.owner_username, booking.created_by_username) == (name, name), (
            kind
        )

        for env in (
            env_listed[rows["environments"][kind]],
            await _environments.get(async_session, rows["environments"][kind]),
        ):
            assert (env.owner_username, env.created_by_username) == (name, name), kind
            assert [child.owner_username for child in env.bookings] == [name], kind
        ns = rows["namespaces"][kind]
        [by_ns] = await _environments.get_by_namespace(
            async_session, ns.name, ns.cluster_name
        )
        assert (by_ns.owner_username, by_ns.created_by_username) == (name, name), kind
        assert [child.owner_username for child in by_ns.bookings] == [name], kind

        assert ns_held[ns.id] == name, kind
        assert svm_held[rows["static_vms"][kind]] == name, kind

    no_creator = listed[references["no_creator"]]
    assert no_creator.created_by_username is None
    assert no_creator.owner_username == references["alive"].username


@pytest.mark.parametrize("collation", [None, "und-x-icu"])
async def test_guard_ignores_the_reference_collation(
    async_session, references, collation
):
    """Non-ASCII lookalikes resolve to no user and raise nothing, whatever the reference's
    collation (the guard matches under COLLATE "C")."""
    if collation is not None:
        present = (
            await async_session.execute(
                text("SELECT count(*) FROM pg_collation WHERE collname = :c"),
                {"c": collation},
            )
        ).scalar_one()
        if not present:
            pytest.skip(f"collation {collation} not available on this server")
    ids = [
        references["rows"]["bookings"][k]
        for k in ("non_ascii_digit", "non_ascii_letter", "canonical")
    ]
    ref = (
        BookingModel.user_id
        if collation is None
        else BookingModel.user_id.collate(collation)
    )
    resolved = dict(
        (
            await async_session.execute(
                select(BookingModel.id, user_ref_uuid(ref)).where(
                    BookingModel.id.in_(ids)
                )
            )
        ).all()
    )
    assert resolved == {ids[0]: None, ids[1]: None, ids[2]: references["alive"].id}


# ── 4.2 / 4.3 plans ───────────────────────────────────────────────────────────────────────────


async def _vacuum(engine: AsyncEngine, sql: str) -> None:
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text(sql))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def page(async_engine: AsyncEngine):
    """20,000 committed, analysed users; a 50-row environments page and a 50-row bookings page
    with 50 distinct owners, and 6 creator slots of which 4 repeat one creator."""
    tag = f"p510{uuid4().hex[:8]}"
    await _vacuum(async_engine, "VACUUM users, environments, bookings")
    async with AsyncSession(async_engine) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, created_at) "
                "SELECT gen_random_uuid(), :tag || '-' || g, 'x', 'user', now() "
                "FROM generate_series(1, 20000) g"
            ),
            {"tag": tag},
        )
        user_ids = [
            str(u)
            for u in (
                await session.execute(
                    text(
                        "SELECT id FROM users WHERE username LIKE :p ORDER BY username LIMIT 53"
                    ),
                    {"p": f"{tag}-%"},
                )
            ).scalars()
        ]
        owners, repeated, others = user_ids[:50], user_ids[50], user_ids[51:53]
        creators = [repeated] * 4 + others + [None] * 44
        env_rows, booking_rows = [], []
        for i in range(50):
            at = _BASE + timedelta(days=1, seconds=i)
            common = {
                "user_id": owners[i],
                "created_by": creators[i],
                "ttl_minutes": 60,
                "expires_at": PERMANENT_EXPIRES_AT,
                "created_at": at,
            }
            env_rows.append(
                {
                    "id": uuid4(),
                    "name": f"{tag}-{i}",
                    "construction_complete": True,
                    **common,
                }
            )
            booking_rows.append(
                {"id": uuid4(), "status": "READY", "resource_type": "VM", **common}
            )
        await session.execute(insert(EnvironmentModel), env_rows)
        await session.execute(insert(BookingModel), booking_rows)
        await session.commit()
    await _vacuum(async_engine, "VACUUM ANALYZE users, environments, bookings")

    yield {
        "env_ids": [r["id"] for r in env_rows],
        "booking_ids": [r["id"] for r in booking_rows],
        "owner_slots": 50,
        "creator_slots": sum(c is not None for c in creators),
    }

    async with AsyncSession(async_engine) as session:
        await session.execute(
            delete(BookingModel).where(
                BookingModel.id.in_([r["id"] for r in booking_rows])
            )
        )
        await session.execute(
            delete(EnvironmentModel).where(
                EnvironmentModel.id.in_([r["id"] for r in env_rows])
            )
        )
        await session.execute(
            delete(UserModel).where(UserModel.username.like(f"{tag}-%"))
        )
        await session.commit()
    # Leave no statistics of the deleted rows behind: later plan tests read their own.
    await _vacuum(async_engine, "VACUUM ANALYZE users, environments, bookings")


def _literal(value) -> str:
    return (
        "'"
        + (value.isoformat() if isinstance(value, datetime) else str(value)).replace(
            "'", "''"
        )
        + "'"
    )


async def _explain(
    session: AsyncSession, stmt, *, plan_mode: str, seqscan: bool
) -> dict:
    """EXPLAIN ANALYZE `stmt` as a PREPAREd statement under `plan_mode`; the top plan node."""
    compiled = stmt.compile(
        dialect=asyncpg_dialect(), compile_kwargs={"render_postcompile": True}
    )
    args = ", ".join(_literal(compiled.params[name]) for name in compiled.positiontup)
    await session.execute(text(f"SET LOCAL plan_cache_mode = {plan_mode}"))
    if not seqscan:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(text(f"PREPARE names_510 AS {compiled}"))
    try:
        [plan] = (
            await session.execute(
                text(f"EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE names_510({args})")
            )
        ).scalars()
    finally:
        await session.execute(text("DEALLOCATE names_510"))
        await session.execute(text("RESET plan_cache_mode"))
        await session.execute(text("RESET enable_seqscan"))
    plan = plan if isinstance(plan, list) else json.loads(plan)
    return plan[0]["Plan"]


def _nodes(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _users_reads(plan: dict) -> dict[str, list[dict]]:
    reads: dict[str, list[dict]] = {}
    for node in _nodes(plan):
        if node.get("Relation Name") == "users":
            reads.setdefault(node["Alias"], []).append(node)
    return reads


def _assert_users_by_primary_key(plan: dict, slots: dict[str, int]) -> None:
    reads = _users_reads(plan)
    assert set(reads) == set(slots), reads.keys()
    for alias, nodes in reads.items():
        for node in nodes:
            assert node["Node Type"] == "Index Scan", (alias, node["Node Type"])
            assert node["Index Name"] == "users_pkey", (alias, node.get("Index Name"))
            assert "CASE WHEN" in node["Index Cond"], (alias, node["Index Cond"])
        read = sum(n["Actual Loops"] * n["Actual Rows"] for n in nodes)
        assert read <= slots[alias], (alias, read, slots[alias])


def _statements(page: dict):
    slots = {"users": page["owner_slots"], "users_1": page["creator_slots"]}
    return {
        "environments": (
            _with_usernames().where(EnvironmentModel.id.in_(page["env_ids"])),
            slots,
        ),
        "bookings": (_list_items_by_ids_stmt(page["booking_ids"]), slots),
    }


@pytest.mark.parametrize("plan_mode", _PLAN_MODES)
@pytest.mark.parametrize("read", ["environments", "bookings"])
async def test_primary_key_lookup_is_available(async_engine, page, plan_mode, read):
    """Spec, second requirement: with sequential scans off, users are read only through
    users_pkey with the reference as the index condition, one probe per reference occurrence."""
    stmt, slots = _statements(page)[read]
    async with AsyncSession(async_engine) as session:
        plan = await _explain(session, stmt, plan_mode=plan_mode, seqscan=False)
        await session.rollback()
    _assert_users_by_primary_key(plan, slots)


@pytest.mark.parametrize("plan_mode", _PLAN_MODES)
@pytest.mark.parametrize("read", ["environments", "bookings"])
async def test_planner_chooses_the_lookup_at_20k_users(
    async_engine, page, plan_mode, read
):
    """Measured regression, NOT a spec guarantee (design Decision 5): on the CI baseline
    (PostgreSQL 15) with 20,000 analysed users and default settings, the planner's own plan is
    the per-row users_pkey probe. A failure here means planner behaviour changed — review it;
    the spec only promises that the lookup is available."""
    stmt, slots = _statements(page)[read]
    async with AsyncSession(async_engine) as session:
        plan = await _explain(session, stmt, plan_mode=plan_mode, seqscan=True)
        await session.rollback()
    _assert_users_by_primary_key(plan, slots)


# ── 4.4 username filters ──────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def holders(async_session: AsyncSession):
    tag = uuid4().hex[:8]
    alice = UserModel(
        id=uuid4(), username=f"alice-{tag}", password_hash="x", role="user"
    )
    bob = UserModel(id=uuid4(), username=f"bob-{tag}", password_hash="x", role="user")
    held = [
        NamespaceModel(id=uuid4(), name=f"h510-{tag}-{i}", cluster_name=f"h510-{tag}")
        for i in range(2)
    ]
    free = NamespaceModel(
        id=uuid4(), name=f"h510-{tag}-free", cluster_name=f"h510-{tag}"
    )
    async_session.add_all([alice, bob, *held, free])
    await async_session.flush()
    async_session.add_all(
        [
            _booking(
                str(alice.id),
                None,
                _BASE,
                resource_type="NAMESPACE",
                namespace_id=ns.id,
            )
            for ns in held
        ]
    )
    await async_session.flush()
    return {
        "alice": alice.username,
        "bob": bob.username,
        "held": {ns.id for ns in held},
        "ours": {ns.id for ns in held} | {free.id},
    }


async def test_held_by_username(async_session, holders):
    held = {
        ns.id
        for ns in await _namespaces.list_held_by_username(
            async_session, holders["alice"]
        )
    }
    assert held == holders["held"]
    assert await _namespaces.list_held_by_username(async_session, holders["bob"]) == []
    assert await _namespaces.list_held_by_username(async_session, "nobody-510") == []


async def test_not_held_by_username(async_session, holders):
    active = {ns.id for ns in await _namespaces.list_active(async_session)}
    not_alice = {
        ns.id
        for ns in await _namespaces.list_active_not_held_by_username(
            async_session, holders["alice"]
        )
    }
    assert not_alice == active - holders["held"]
    not_bob = {
        ns.id
        for ns in await _namespaces.list_active_not_held_by_username(
            async_session, holders["bob"]
        )
    }
    assert not_bob == active
    nobody = {
        ns.id
        for ns in await _namespaces.list_active_not_held_by_username(
            async_session, "nobody-510"
        )
    }
    assert nobody == active and holders["ours"] <= nobody


async def test_username_filters_read_one_user_by_username(
    async_session, holders, async_engine
):
    """Spec, third requirement: the username is resolved once, through users_username_key."""
    statements: list[tuple[str, tuple]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM namespaces" in statement:
            statements.append((statement, parameters))

    from sqlalchemy import event

    event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
    try:
        await _namespaces.list_held_by_username(async_session, holders["alice"])
        await _namespaces.list_active_not_held_by_username(
            async_session, holders["alice"]
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    conn = await async_session.connection()
    for statement, parameters in statements:
        [plan] = (
            await conn.exec_driver_sql(
                f"EXPLAIN (ANALYZE, FORMAT JSON) {statement}", parameters
            )
        ).scalars()
        plan = plan if isinstance(plan, list) else json.loads(plan)
        users = [
            n for n in _nodes(plan[0]["Plan"]) if n.get("Relation Name") == "users"
        ]
        assert users, statement
        for node in users:
            assert node["Node Type"] in ("Index Scan", "Index Only Scan"), node[
                "Node Type"
            ]
            assert node["Index Name"] == "users_username_key"
            assert node["Actual Loops"] * node["Actual Rows"] <= 1
