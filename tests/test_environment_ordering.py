"""Tests for environment ordering (v0.8.0 P3.2, #209).

Ordering a blueprint creates a parent Environment + child bookings (tagged environment_id, shared
TTL). The use case is tested with stubbed booking use cases; status derivation + API gating too.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BlueprintNotFoundError, EnvironmentItemError, NamespaceUnavailableError, QuotaExceededError,
)


def _bp_item(rt, spec, label=None, pos=0):
    return EnvironmentBlueprintItem(id=uuid4(), resource_type=rt, position=pos, label=label, spec=spec)


def _blueprint(items):
    return EnvironmentBlueprint(
        id=uuid4(), name="dev-stack", description=None, is_active=True,
        created_at=datetime.now(timezone.utc), items=items,
    )


def _booking(rt=ResourceType.VM, status=BookingStatus.PENDING, env_id=None):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=status, resource_type=rt, ttl_minutes=240,
        expires_at=now + timedelta(minutes=240), created_at=now, environment_id=env_id,
    )


def _make_use_case(blueprint, create_returns=None, static_returns=None, ns_returns=None):
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc))
    env_repo = MagicMock()
    env_repo.create = AsyncMock(return_value=env)
    env_repo.get = AsyncMock(return_value=env)
    env_repo.delete = AsyncMock()
    env_repo.start_lease_if_ready = AsyncMock(return_value=False)
    blueprint_repo = MagicMock()
    blueprint_repo.get_by_name = AsyncMock(return_value=blueprint)
    booking_repo = MagicMock()
    booking_repo.update_status = AsyncMock()
    booking_repo.get_live_standalone_namespace_booking = AsyncMock(return_value=None)
    create_uc = MagicMock()
    create_uc.execute = AsyncMock(side_effect=create_returns or (lambda *a, **k: _booking(env_id=env.id)))
    static_uc = MagicMock()
    static_uc.execute = AsyncMock(side_effect=static_returns or (lambda *a, **k: _booking(ResourceType.STATIC_VM, BookingStatus.READY, env.id)))
    ns_uc = MagicMock()
    ns_uc.execute = AsyncMock(side_effect=ns_returns or (lambda *a, **k: _booking(ResourceType.NAMESPACE, BookingStatus.READY, env.id)))
    image_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    hw_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    role_repo = MagicMock(get_by_name=AsyncMock(
        return_value=SimpleNamespace(name="docker-machine", ansible_role="docker_machine", default_vars={})))
    svm_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    dispatcher = MagicMock()
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, booking_repo, create_uc, static_uc, ns_uc,
        image_repo, hw_repo, role_repo, svm_repo, dispatcher,
    )
    return uc, SimpleNamespace(env=env, env_repo=env_repo, create_uc=create_uc, ns_uc=ns_uc,
                               static_uc=static_uc, dispatcher=dispatcher, booking_repo=booking_repo)


@pytest.mark.asyncio
async def test_order_creates_environment_and_children():
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium", "roles": ["docker-machine"]}, "web", 1),
    ])
    uc, m = _make_use_case(bp)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")

    m.env_repo.create.assert_awaited_once()
    m.ns_uc.execute.assert_awaited_once()
    m.create_uc.execute.assert_awaited_once()
    # VM child created with dispatch deferred + environment_id + resolved roles.
    kwargs = m.create_uc.execute.call_args.kwargs
    assert kwargs["dispatch"] is False
    assert kwargs["environment_id"] == m.env.id
    assert kwargs["config_roles"][0]["ansible_role"] == "docker_machine"
    # Provisioning dispatched once, after all children created.
    m.dispatcher.dispatch_provision.assert_called_once()


@pytest.mark.asyncio
async def test_order_unknown_blueprint_404():
    uc, m = _make_use_case(None)
    uc._blueprint_repo.get_by_name = AsyncMock(return_value=None)
    with pytest.raises(BlueprintNotFoundError):
        await uc.execute(MagicMock(), "nope", 240, user_id="u")
    m.env_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_order_unknown_item_name_creates_nothing():
    bp = _blueprint([_bp_item("VM", {"image_name": "nope", "hw_config_name": "medium"}, "web", 0)])
    uc, m = _make_use_case(bp)
    uc._image_repo.get_by_name = AsyncMock(return_value=None)  # unknown image
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    m.env_repo.create.assert_not_called()       # resolution happens before creating the env
    m.create_uc.execute.assert_not_called()


@pytest.mark.asyncio
async def test_order_quota_failure_rolls_back():
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "web", 1),
    ])
    uc, m = _make_use_case(bp, create_returns=QuotaExceededError("quota"))
    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    # The namespace child created before the VM failure is released; env deleted; nothing dispatched.
    m.booking_repo.update_status.assert_awaited()  # released the created child
    m.env_repo.delete.assert_awaited_once()
    m.dispatcher.dispatch_provision.assert_not_called()


# ── Namespace override (#235) ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_order_override_by_namespace_id():
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "web", 1),
    ])
    uc, m = _make_use_case(bp)
    chosen = uuid4()
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_id=chosen)
    # The chosen namespace id is threaded through to BookNamespaceUseCase.
    assert m.ns_uc.execute.call_args.kwargs["namespace_id"] == chosen
    m.create_uc.execute.assert_awaited_once()   # other children unaffected
    m.env_repo.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_order_override_by_name_and_cluster():
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "web", 1),
    ])
    uc, m = _make_use_case(bp)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                     namespace_name="dev1", cluster_name="prod-cluster")
    kwargs = m.ns_uc.execute.call_args.kwargs
    assert kwargs["namespace_name"] == "dev1"
    assert kwargs["cluster_name"] == "prod-cluster"


@pytest.mark.asyncio
async def test_order_override_no_namespace_item_creates_nothing():
    bp = _blueprint([_bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "web", 0)])
    uc, m = _make_use_case(bp)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_name="dev1",
                         cluster_name="prod-cluster")
    m.env_repo.create.assert_not_called()   # guard runs before creating the env
    m.create_uc.execute.assert_not_called()
    m.ns_uc.execute.assert_not_called()


@pytest.mark.asyncio
async def test_order_override_two_namespace_items_creates_nothing():
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns1", 0),
        _bp_item("NAMESPACE", {}, "ns2", 1),
    ])
    uc, m = _make_use_case(bp)
    with pytest.raises(EnvironmentItemError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_id=uuid4())
    m.env_repo.create.assert_not_called()
    m.ns_uc.execute.assert_not_called()


@pytest.mark.asyncio
async def test_order_override_unknown_namespace_rolls_back():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case(bp, ns_returns=NamespaceUnavailableError("no such namespace"))
    with pytest.raises(NamespaceUnavailableError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                         namespace_name="ghost", cluster_name="prod-cluster")
    m.env_repo.delete.assert_awaited_once()           # whole environment rolled back
    m.dispatcher.dispatch_provision.assert_not_called()


@pytest.mark.asyncio
async def test_order_without_override_unchanged():
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case(bp)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    kwargs = m.ns_uc.execute.call_args.kwargs
    assert kwargs["namespace_id"] is None
    assert kwargs["namespace_name"] is None
    assert kwargs["cluster_name"] is None


# ── Derived status ─────────────────────────────────────────────────────────────
def test_derived_status():
    from app.presentation.routes.api_environments import _derived_status
    env = lambda sts: Environment(  # noqa: E731
        id=uuid4(), name="e", blueprint_name=None, user_id="u", ttl_minutes=1,
        expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
        bookings=[_booking(status=s) for s in sts],
    )
    assert _derived_status(env([BookingStatus.READY, BookingStatus.PROVISIONING])) == "PROVISIONING"
    assert _derived_status(env([BookingStatus.READY, BookingStatus.READY])) == "READY"
    assert _derived_status(env([BookingStatus.READY, BookingStatus.FAILED])) == "FAILED"
    assert _derived_status(env([BookingStatus.RELEASED, BookingStatus.RELEASED])) == "RELEASED"


# ── API ─────────────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient_app()
    app.dependency_overrides.clear()


def TestClient_app():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_api_order_environment_201(client):
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
                      bookings=[_booking(ResourceType.NAMESPACE, BookingStatus.READY),
                                _booking(ResourceType.VM, BookingStatus.PROVISIONING)])
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.post("/api/environments", json={"blueprint_name": "dev-stack", "ttl_minutes": 240})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "dev-stack"
    assert body["status"] == "PROVISIONING"   # a child is in-flight
    assert len(body["bookings"]) == 2


def test_api_order_unknown_blueprint_404(client):
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(side_effect=BlueprintNotFoundError("nope"))
        resp = client.post("/api/environments", json={"blueprint_name": "nope", "ttl_minutes": 240})
    assert resp.status_code == 404


def test_api_order_namespace_override_201(client):
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
                      bookings=[_booking(ResourceType.NAMESPACE, BookingStatus.READY)])
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.post("/api/environments", json={
            "blueprint_name": "dev-stack", "ttl_minutes": 240,
            "namespace_name": "dev1", "cluster_name": "prod-cluster"})
    assert resp.status_code == 201
    kwargs = uc.execute.call_args.kwargs
    assert kwargs["namespace_name"] == "dev1"
    assert kwargs["cluster_name"] == "prod-cluster"


def test_api_order_one_of_pair_400(client):
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock()
        resp = client.post("/api/environments", json={
            "blueprint_name": "dev-stack", "ttl_minutes": 240, "namespace_name": "dev1"})
    assert resp.status_code == 400
    uc.execute.assert_not_called()   # validated before reaching the use case


def test_api_order_no_namespace_blueprint_override_400(client):
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentItemError("this blueprint has no namespace to choose"))
        resp = client.post("/api/environments", json={
            "blueprint_name": "vm-only", "ttl_minutes": 240,
            "namespace_name": "dev1", "cluster_name": "prod-cluster"})
    assert resp.status_code == 400


def test_api_order_unknown_pair_409(client):
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(side_effect=NamespaceUnavailableError("no such namespace"))
        resp = client.post("/api/environments", json={
            "blueprint_name": "dev-stack", "ttl_minutes": 240,
            "namespace_name": "ghost", "cluster_name": "prod-cluster"})
    assert resp.status_code == 409


def test_api_get_environment_200_for_any_authenticated_user(client):
    env = Environment(id=uuid4(), name="e", blueprint_name=None, user_id="someone-else",
                      ttl_minutes=1, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc))
    with patch("app.presentation.routes.api_environments._env_repo") as repo:
        repo.get = AsyncMock(return_value=env)
        resp = client.get(f"/api/environments/{env.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(env.id)


# ── Namespace adoption (#adopt-existing-namespace) ─────────────────────────────


def _make_use_case_with_ns_repo(
    blueprint, *, standalone_booking=None, create_returns=None, ns_returns=None,
    ns_obj=None,
):
    """Like _make_use_case but with a namespace_repo wired in for adoption tests."""
    now = datetime.now(timezone.utc)
    env = Environment(id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
                      ttl_minutes=240, expires_at=now + timedelta(hours=12),
                      created_at=now)
    env_repo = MagicMock()
    env_repo.create = AsyncMock(return_value=env)
    env_repo.get = AsyncMock(return_value=env)
    env_repo.delete = AsyncMock()
    env_repo.start_lease_if_ready = AsyncMock(return_value=False)
    blueprint_repo = MagicMock()
    blueprint_repo.get_by_name = AsyncMock(return_value=blueprint)

    booking_repo = MagicMock()
    booking_repo.update_status = AsyncMock()
    booking_repo.set_environment = AsyncMock()
    booking_repo.get_live_standalone_namespace_booking = AsyncMock(return_value=standalone_booking)

    # booking_repo.get returns the standalone booking (after adoption) if there is one.
    def _get_side_effect(session, bid):
        if standalone_booking is not None and bid == standalone_booking.id:
            # Return a version of the booking that now has env_id set.
            updated = Booking(
                id=standalone_booking.id,
                user_id=standalone_booking.user_id,
                status=standalone_booking.status,
                resource_type=standalone_booking.resource_type,
                ttl_minutes=240,
                expires_at=env.expires_at,
                created_at=standalone_booking.created_at,
                environment_id=env.id,
                environment_label="ns",
            )
            return updated
        raise ValueError(f"unexpected get({bid})")

    booking_repo.get = AsyncMock(side_effect=_get_side_effect)

    create_uc = MagicMock()
    create_uc.execute = AsyncMock(side_effect=create_returns or (lambda *a, **k: _booking(env_id=env.id)))
    static_uc = MagicMock()
    static_uc.execute = AsyncMock(side_effect=lambda *a, **k: _booking(ResourceType.STATIC_VM, BookingStatus.READY, env.id))
    ns_uc = MagicMock()
    ns_uc.execute = AsyncMock(side_effect=ns_returns or (lambda *a, **k: _booking(ResourceType.NAMESPACE, BookingStatus.READY, env.id)))

    image_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    hw_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    role_repo = MagicMock(get_by_name=AsyncMock(
        return_value=SimpleNamespace(name="docker-machine", ansible_role="docker_machine", default_vars={})))
    svm_repo = MagicMock(get_by_name=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    dispatcher = MagicMock()

    namespace_repo = MagicMock()
    namespace_repo.get_by_name_and_cluster = AsyncMock(return_value=ns_obj)
    namespace_repo.list_held_standalone_by_user = AsyncMock(return_value=[])

    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    uc = OrderEnvironmentUseCase(
        env_repo, blueprint_repo, booking_repo, create_uc, static_uc, ns_uc,
        image_repo, hw_repo, role_repo, svm_repo, dispatcher, namespace_repo,
    )
    return uc, SimpleNamespace(
        env=env, env_repo=env_repo, create_uc=create_uc, ns_uc=ns_uc,
        static_uc=static_uc, dispatcher=dispatcher, booking_repo=booking_repo,
        namespace_repo=namespace_repo,
    )


def _standalone_ns_booking(ns_id):
    """A READY, standalone (no env) namespace booking."""
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.READY,
        resource_type=ResourceType.NAMESPACE, ttl_minutes=120,
        expires_at=now + timedelta(hours=2), created_at=now,
        namespace_id=ns_id,
    )


@pytest.mark.asyncio
async def test_adopt_held_standalone_namespace():
    """Chosen namespace is held standalone by owner → adopted (set_environment called, not ns_uc)."""
    ns_id = uuid4()
    standalone = _standalone_ns_booking(ns_id)
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case_with_ns_repo(bp, standalone_booking=standalone)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_id=ns_id)

    # set_environment called to adopt the booking into the new environment.
    m.booking_repo.set_environment.assert_awaited_once()
    call_kwargs = m.booking_repo.set_environment.call_args
    # env_id and label set, not None.
    assert call_kwargs.args[2] == m.env.id   # environment_id positional
    assert call_kwargs.args[3] == "ns"        # environment_label positional

    # book_namespace use case NOT called — no new reservation.
    m.ns_uc.execute.assert_not_called()


@pytest.mark.asyncio
async def test_no_adopt_if_namespace_not_held_standalone():
    """Namespace is free (no standalone booking) → normal reserve path, no set_environment."""
    ns_id = uuid4()
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    # standalone_booking=None means get_live_standalone_namespace_booking returns None
    uc, m = _make_use_case_with_ns_repo(bp, standalone_booking=None)
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_id=ns_id)

    m.ns_uc.execute.assert_awaited_once()
    m.booking_repo.set_environment.assert_not_called()


@pytest.mark.asyncio
async def test_rollback_detaches_adopted_namespace():
    """Adoption + later child failure → adopted booking is detached (set_environment with None env_id),
    not released."""
    ns_id = uuid4()
    standalone = _standalone_ns_booking(ns_id)
    bp = _blueprint([
        _bp_item("NAMESPACE", {}, "ns", 0),
        _bp_item("VM", {"image_name": "Ubuntu", "hw_config_name": "medium"}, "web", 1),
    ])
    uc, m = _make_use_case_with_ns_repo(
        bp, standalone_booking=standalone,
        create_returns=QuotaExceededError("quota"),
    )
    with pytest.raises(QuotaExceededError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u", namespace_id=ns_id)

    # set_environment called twice: once to adopt, once to detach on rollback.
    assert m.booking_repo.set_environment.await_count == 2
    # Second call (detach): environment_id is None.
    detach_call = m.booking_repo.set_environment.call_args_list[1]
    assert detach_call.args[2] is None   # environment_id = None on detach

    # The adopted booking is NOT released (update_status not called for it).
    for call in m.booking_repo.update_status.call_args_list:
        assert call.args[2] != standalone.id

    # Environment row deleted.
    m.env_repo.delete.assert_awaited_once()
    m.dispatcher.dispatch_provision.assert_not_called()


@pytest.mark.asyncio
async def test_adoption_via_name_cluster_resolves_namespace():
    """Adoption works when namespace is specified by name+cluster (resolved via namespace_repo)."""
    ns_id = uuid4()
    standalone = _standalone_ns_booking(ns_id)
    ns_obj = SimpleNamespace(id=ns_id, name="dev1", cluster_name="prod-cluster")
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    uc, m = _make_use_case_with_ns_repo(bp, standalone_booking=standalone, ns_obj=ns_obj)

    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                     namespace_name="dev1", cluster_name="prod-cluster")

    # Resolved via namespace_repo.
    m.namespace_repo.get_by_name_and_cluster.assert_awaited_once_with(
        ANY, "dev1", "prod-cluster",
    )
    m.booking_repo.set_environment.assert_awaited_once()
    m.ns_uc.execute.assert_not_called()


@pytest.mark.asyncio
async def test_name_cluster_namespace_not_found_raises():
    """namespace_name+cluster_name that resolves to None → NamespaceUnavailableError."""
    bp = _blueprint([_bp_item("NAMESPACE", {}, "ns", 0)])
    # ns_obj=None: the namespace doesn't exist in the catalog
    uc, m = _make_use_case_with_ns_repo(bp, standalone_booking=None, ns_obj=None)

    from app.domain.exceptions import NamespaceUnavailableError
    with pytest.raises(NamespaceUnavailableError):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u",
                         namespace_name="ghost", cluster_name="prod-cluster")

    # Nothing created.
    m.env_repo.create.assert_not_called()


# ── API adoption tests ─────────────────────────────────────────────────────────

def test_api_adopt_held_namespace_201(client):
    """A namespace held standalone by the user → 201 (use case handles adoption internally)."""
    ns_id = uuid4()
    now = datetime.now(timezone.utc)
    env = Environment(
        id=uuid4(), name="dev-stack", blueprint_name="dev-stack", user_id="u",
        ttl_minutes=240, expires_at=now, created_at=now,
        bookings=[_booking(ResourceType.NAMESPACE, BookingStatus.READY)],
    )
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(return_value=env)
        resp = client.post("/api/environments", json={
            "blueprint_name": "dev-stack", "ttl_minutes": 240,
            "namespace_name": "dev1", "cluster_name": "prod-cluster",
        })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "READY"


def test_api_held_by_other_409(client):
    """Namespace held by another user → 409 (NamespaceUnavailableError from use case)."""
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(side_effect=NamespaceUnavailableError("namespace held by another user"))
        resp = client.post("/api/environments", json={
            "blueprint_name": "dev-stack", "ttl_minutes": 240,
            "namespace_name": "dev1", "cluster_name": "prod-cluster",
        })
    assert resp.status_code == 409
