"""Tests for ordering an environment on a specific namespace (409 if busy)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import EnvironmentItemError, NamespaceUnavailableError


def _bp_item(rt, spec, label=None, pos=0):
    return EnvironmentBlueprintItem(id=uuid4(), resource_type=rt, position=pos, label=label, spec=spec)


def _blueprint(items):
    return EnvironmentBlueprint(
        id=uuid4(), name="dev-stack", description=None, is_active=True,
        created_at=datetime.now(timezone.utc), items=items,
    )


def _booking(rt, status, env_id):
    now = datetime.now(timezone.utc)
    return Booking(id=uuid4(), user_id="u", status=status, resource_type=rt, ttl_minutes=240,
                   expires_at=now + timedelta(minutes=240), created_at=now, environment_id=env_id)


def _ns(name="dev1", cluster="c1"):
    return SimpleNamespace(id=uuid4(), name=name, cluster_name=cluster, api_url=None)


def _make_use_case(blueprint, namespace_repo, ns_busy=False):
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc))
    env_repo = MagicMock(create=AsyncMock(return_value=env), get=AsyncMock(return_value=env),
                         delete=AsyncMock(), start_lease_if_ready=AsyncMock(return_value=False))
    blueprint_repo = MagicMock(get_by_name=AsyncMock(return_value=blueprint))
    booking_repo = MagicMock(update_status=AsyncMock())
    create_uc = MagicMock(execute=AsyncMock(side_effect=lambda *a, **k: _booking(ResourceType.VM, BookingStatus.PENDING, env.id)))
    static_uc = MagicMock(execute=AsyncMock())
    ns_side = (NamespaceUnavailableError("namespace 'dev1' is already booked") if ns_busy
               else None)
    ns_uc = MagicMock(execute=AsyncMock(
        side_effect=ns_side or (lambda *a, **k: _booking(ResourceType.NAMESPACE, BookingStatus.READY, env.id))))
    image_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    hw_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    role_repo = MagicMock(get_by_name=AsyncMock())
    svm_repo = MagicMock(get_by_name=AsyncMock())
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, booking_repo, create_uc, static_uc, ns_uc,
        image_repo, hw_repo, role_repo, svm_repo, MagicMock(), namespace_repo=namespace_repo,
    )
    return uc, SimpleNamespace(env=env, env_repo=env_repo, ns_uc=ns_uc)


# ── Pinning a free namespace ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pin_free_namespace_is_used():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[_ns("dev1", "c1")]))
    uc, m = _make_use_case(bp, ns_repo)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1")
    kwargs = m.ns_uc.execute.call_args.kwargs
    assert kwargs["namespace_name"] == "dev1" and kwargs["cluster_name"] == "c1"


@pytest.mark.asyncio
async def test_cluster_qualified_pin():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name_and_cluster=AsyncMock(return_value=_ns("dev1", "prod")))
    uc, m = _make_use_case(bp, ns_repo)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                     namespace_name="dev1", cluster_name="prod")
    assert m.ns_uc.execute.call_args.kwargs["cluster_name"] == "prod"
    ns_repo.get_by_name_and_cluster.assert_awaited_once()


# ── Busy namespace → NamespaceUnavailableError (→ 409), rolled back ──────────────
@pytest.mark.asyncio
async def test_busy_namespace_raises_and_rolls_back():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[_ns("dev1", "c1")]))
    uc, m = _make_use_case(bp, ns_repo, ns_busy=True)
    with pytest.raises(NamespaceUnavailableError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1")
    m.env_repo.delete.assert_awaited_once()  # whole order rolled back


# ── Bad/ambiguous name → EnvironmentItemError (→ 400) ───────────────────────────
@pytest.mark.asyncio
async def test_unknown_namespace_raises_item_error():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[]))
    uc, m = _make_use_case(bp, ns_repo)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="ghost")
    m.env_repo.create.assert_not_awaited()  # nothing created


@pytest.mark.asyncio
async def test_ambiguous_namespace_raises_item_error():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[_ns("dev1", "a"), _ns("dev1", "b")]))
    uc, _ = _make_use_case(bp, ns_repo)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1")


@pytest.mark.asyncio
async def test_cluster_qualified_unknown_raises_item_error():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name_and_cluster=AsyncMock(return_value=None))
    uc, _ = _make_use_case(bp, ns_repo)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                         namespace_name="dev1", cluster_name="nope")


# ── Blueprint must have exactly one namespace item ──────────────────────────────
@pytest.mark.asyncio
async def test_no_namespace_item_raises_item_error():
    bp = _blueprint([_bp_item("VM", {"image_name": "U", "hw_config_name": "m"}, "web", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[_ns()]))
    uc, _ = _make_use_case(bp, ns_repo)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1")


@pytest.mark.asyncio
async def test_multiple_namespace_items_raises_item_error():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "a", 0), _bp_item("NAMESPACE", {}, "b", 1)])
    ns_repo = MagicMock(get_by_name=AsyncMock(return_value=[_ns()]))
    uc, _ = _make_use_case(bp, ns_repo)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1")


# ── Omitting namespace_name is unchanged (regression) ───────────────────────────
@pytest.mark.asyncio
async def test_omitting_pin_leaves_blueprint_behaviour():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    ns_repo = MagicMock(get_by_name=AsyncMock())
    uc, m = _make_use_case(bp, ns_repo)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    ns_repo.get_by_name.assert_not_called()  # no resolution when not pinning
    # namespace child still ordered from the (empty) spec → any-available
    assert m.ns_uc.execute.call_args.kwargs["namespace_name"] is None


# ── Route maps the errors (409 busy / 400 bad) ──────────────────────────────────
def _client(user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    from fastapi.testclient import TestClient
    return TestClient(app), app


def _user(role="dispatcher"):
    return User(id=uuid4(), username="ci", password_hash="", role=role,
                is_active=True, created_at=datetime.now(timezone.utc))


def test_route_busy_namespace_is_409():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes._dispatch._user_repo") as urepo, \
             patch("app.presentation.routes.api_environments._order_use_case") as uc:
            urepo.get_by_username = AsyncMock(return_value=_user("user"))
            uc.execute = AsyncMock(side_effect=NamespaceUnavailableError("namespace 'dev1' is already booked"))
            resp = cl.post("/api/environments", json={
                "blueprint_name": "dev-stack", "ttl_minutes": 240,
                "on_behalf_of": "john", "namespace_name": "dev1",
            })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert "already booked" in resp.json()["detail"]


def test_route_unknown_namespace_is_400():
    cl, app = _client(_user())
    try:
        with patch("app.presentation.routes.api_environments._order_use_case") as uc:
            uc.execute = AsyncMock(side_effect=EnvironmentItemError("no namespace 'ghost'"))
            resp = cl.post("/api/environments", json={
                "blueprint_name": "dev-stack", "ttl_minutes": 240, "namespace_name": "ghost",
            })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
