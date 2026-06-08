"""Tests for environment ordering (v0.8.0 P3.2, #209).

Ordering a blueprint creates a parent Environment + child bookings (tagged environment_id, shared
TTL). The use case is tested with stubbed booking use cases; status derivation + API gating too.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import Booking, Environment, EnvironmentBlueprint, EnvironmentBlueprintItem
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BlueprintNotFoundError, EnvironmentItemError, QuotaExceededError


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
    blueprint_repo = MagicMock()
    blueprint_repo.get_by_name = AsyncMock(return_value=blueprint)
    booking_repo = MagicMock()
    booking_repo.update_status = AsyncMock()
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


def test_api_get_environment_403_for_non_owner(client):
    env = Environment(id=uuid4(), name="e", blueprint_name=None, user_id="someone-else",
                      ttl_minutes=1, expires_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc))
    with patch("app.presentation.routes.api_environments._env_repo") as repo:
        repo.get = AsyncMock(return_value=env)
        resp = client.get(f"/api/environments/{env.id}")
    assert resp.status_code == 403
