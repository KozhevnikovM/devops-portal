"""Integration: what one filter change costs on the list pages (#494).

Measures the statement count and response size of the same filter request (All, Show released, a
label) on the VM, namespace and environments pages, as a user and as an admin, with call-counting
spies on every order-form catalog read. The numbers are printed (run with ``-s``) so they can be
recorded with the change. Only relative facts are asserted — never a wall-clock threshold.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, insert

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import User
from app.infrastructure.auth import require_user
from app.infrastructure.database.models import (
    BookingModel,
    EnvironmentBlueprintModel,
    EnvironmentModel,
    HWConfigModel,
    NamespaceModel,
    RoleModel,
    StaticVMModel,
    UserModel,
    VMImageModel,
)
from app.infrastructure.database.session import get_async_session
from app.main import app
from app.presentation.routes import bookings as booking_routes
from app.presentation.routes import environments as environment_routes

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# Far in the future so the seeded rows are the newest in the test database.
_BASE = datetime(2999, 6, 1, tzinfo=timezone.utc)
_ROWS = 60  # more than the default page size (50), so every list offers Load more
_PASSWORD = "pw-never-in-list-html"

# Every order-form catalog read of the three pages (design D5).
_BOOKING_CATALOGS = [
    (booking_routes._image_repo, "list_active"),
    (booking_routes._hw_config_repo, "list_active"),
    (booking_routes._namespace_repo, "list_available"),
    (booking_routes._static_vm_repo, "list_available"),
    (booking_routes._role_repo, "list_active"),
]
_ENVIRONMENT_CATALOGS = [
    (environment_routes._blueprint_repo, "list_active"),
    (environment_routes._namespace_repo, "list_available"),
    (environment_routes._namespace_repo, "list_held_standalone_by_user"),
]


def _domain_user(row: UserModel) -> User:
    return User(id=row.id, username=row.username, password_hash="", role=row.role,
                is_active=True, created_at=_BASE)


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(async_session):
    token = f"cost{uuid4().hex[:8]}"
    owner = UserModel(id=uuid4(), username=f"{token}-owner", password_hash="x", role="user")
    admin = UserModel(id=uuid4(), username=f"{token}-admin", password_hash="x", role="admin")
    async_session.add_all([owner, admin])

    # A few rows in every catalog, so the page's catalog reads do real work.
    image_ids = [uuid4() for _ in range(5)]
    hw_ids = [uuid4() for _ in range(5)]
    for i in range(5):
        async_session.add(VMImageModel(id=image_ids[i], name=f"{token}-img-{i}",
                                       vapp_template_id="tpl", is_active=True))
        async_session.add(HWConfigModel(id=hw_ids[i], name=f"{token}-hw-{i}", cpus=2,
                                        memory_mb=2048, is_active=True))
        async_session.add(NamespaceModel(name=f"{token}-ns-{i}", cluster_name="c1",
                                         is_active=True))
        async_session.add(StaticVMModel(name=f"{token}-svm-{i}", host=f"10.0.0.{i}",
                                        username="u", password="p", is_active=True))
        async_session.add(RoleModel(name=f"{token}-role-{i}", ansible_role="r", is_active=True))
        async_session.add(EnvironmentBlueprintModel(name=f"{token}-bp-{i}", is_active=True))
    await async_session.flush()

    common = {"user_id": str(owner.id), "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT}
    rows = []
    for i in range(_ROWS):
        created = _BASE - timedelta(seconds=i)
        status = "RELEASED" if i % 5 == 0 else "READY"
        rows.append({**common, "id": uuid4(), "resource_type": "VM", "status": status,
                     "image_id": image_ids[0], "image_name": "ubuntu", "hw_config_id": hw_ids[0],
                     "hw_config_name": "small", "vm_ip": "10.1.0.1", "vm_password": _PASSWORD,
                     "label": f"{token}-vm-{i}", "created_at": created})
        rows.append({**common, "id": uuid4(), "resource_type": "NAMESPACE", "status": status,
                     "label": f"{token}-ns-{i}", "created_at": created})
    await async_session.execute(insert(BookingModel), rows)

    env_rows, child_rows = [], []
    for i in range(_ROWS):
        env_id = uuid4()
        env_rows.append({"id": env_id, "name": f"{token}-env-{i}", "user_id": str(owner.id),
                         "ttl_minutes": 60, "expires_at": PERMANENT_EXPIRES_AT,
                         "construction_complete": True,
                         "created_at": _BASE - timedelta(seconds=i)})
        child_rows.append({**common, "id": uuid4(), "resource_type": "VM", "status": "READY",
                           "environment_id": env_id, "created_at": _BASE - timedelta(seconds=i)})
    await async_session.execute(insert(EnvironmentModel), env_rows)
    await async_session.execute(insert(BookingModel), child_rows)
    await async_session.flush()
    return {"token": token, "owner": _domain_user(owner), "admin": _domain_user(admin)}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@contextmanager
def _catalog_spies(catalogs):
    """Wrap each catalog read in a call-counting spy that still runs the real query."""
    with ExitStack() as stack:
        spies = []
        for repo, method in catalogs:
            spy = AsyncMock(wraps=getattr(repo, method))
            stack.enter_context(patch.object(repo, method, new=spy))
            spies.append((f"{type(repo).__name__}.{method}", spy))
        yield spies


async def _measure(session, user: User, url: str, catalogs) -> dict:
    """One GET as ``user``: statements executed, response bytes, catalog calls."""
    async def _session():
        yield session

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[require_user] = lambda: user
    statements = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = session.bind.engine.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        with _catalog_spies(catalogs) as spies:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                response = await client.get(url)
            catalog_calls = sum(spy.await_count for _, spy in spies)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert response.status_code == 200, response.text[:500]
    return {"statements": len(statements), "bytes": len(response.content),
            "catalog_calls": catalog_calls}


_PAGES = [
    # (name, page route, catalog reads)
    ("vm", "/book/vm", _BOOKING_CATALOGS),
    ("namespace", "/book/namespace", _BOOKING_CATALOGS),
    ("environments", "/environments", _ENVIRONMENT_CATALOGS),
]


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("name,page_path,catalogs", _PAGES, ids=[p[0] for p in _PAGES])
async def test_filter_request_cost_on_page_route(async_session, seeded, role, name, page_path,
                                                 catalogs):
    """Baseline: the filter request as the page route serves it today (the "before")."""
    query = f"?filter=all&show_released=1&label={seeded['token']}"
    page = await _measure(async_session, seeded[role], page_path + query, catalogs)
    print(f"\n[#494 cost] {name:<12} {role:<5} page  {page}")
    assert page["catalog_calls"] > 0


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("name,page_path,catalogs", _PAGES, ids=[p[0] for p in _PAGES])
async def test_list_section_costs_less_than_the_page(async_session, seeded, role, name, page_path,
                                                     catalogs):
    """The same filter request served by the list-section fragment (the "after").

    Relative facts only: fewer statements, fewer bytes, no catalog read. Statements are not banned
    by table name — the list read itself joins static_vms/namespaces to project row fields.
    """
    query = f"?filter=all&show_released=1&label={seeded['token']}"
    page = await _measure(async_session, seeded[role], page_path + query, catalogs)
    fragment = await _measure(async_session, seeded[role], f"{page_path}/list{query}", catalogs)
    print(f"\n[#494 cost] {name:<12} {role:<5} page  {page}"
          f"\n[#494 cost] {name:<12} {role:<5} list  {fragment}")

    assert page["catalog_calls"] > 0
    assert fragment["catalog_calls"] == 0
    assert fragment["statements"] < page["statements"]
    assert fragment["bytes"] < page["bytes"]


async def test_list_section_carries_no_credentials(async_session, seeded):
    """The owner's VM rows hold a password in the database; the fragment HTML never does."""
    query = f"?filter=mine&show_released=1&label={seeded['token']}"
    async def _session():
        yield async_session

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[require_user] = lambda: seeded["owner"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        html = (await client.get(f"/book/vm/list{query}")).text

    assert "Show credentials" in html
    assert _PASSWORD not in html
