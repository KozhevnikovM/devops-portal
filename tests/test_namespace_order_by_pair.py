"""Tests for ordering a namespace by its (name, cluster) pair (#190).

Names are unique per-cluster, so the (namespace_name, cluster_name) pair identifies a namespace.
`POST /api/bookings` accepts the pair and `BookNamespaceUseCase` resolves it to the specific
namespace before reserving it through the existing pooled-reservation flow.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.domain.entities import Booking, Namespace
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import NamespaceUnavailableError


def _ns_booking(namespace_id, name="team-a-dev", cluster="prod-cluster") -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id="dev-user",
        status=BookingStatus.READY,
        resource_type=ResourceType.NAMESPACE,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        namespace_id=namespace_id,
        namespace_name=name,
        cluster_name=cluster,
        api_url="https://api.cluster:6443",
    )


def _namespace(name, cluster) -> Namespace:
    return Namespace(
        id=uuid4(), name=name, cluster_name=cluster, api_url=None,
        is_active=True, created_at=datetime.now(timezone.utc),
    )


# ── Identity model: name unique per-cluster ───────────────────────────────────
def test_namespace_name_is_unique_per_cluster_not_globally():
    """The model identity is the (name, cluster) pair, not the name alone."""
    from sqlalchemy import UniqueConstraint
    from app.infrastructure.database.models import NamespaceModel

    name_col = NamespaceModel.__table__.c.name
    assert not name_col.unique, "name must not be globally unique anymore"

    composite = [
        tuple(c.name for c in con.columns)
        for con in NamespaceModel.__table__.constraints
        if isinstance(con, UniqueConstraint)
    ]
    assert ("name", "cluster_name") in composite


# ── Use case: pair resolution ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_use_case_resolves_pair_to_specific_namespace():
    ns = _namespace("team-a-dev", "prod-cluster")
    ns_model = SimpleNamespace(id=ns.id, name=ns.name, cluster_name=ns.cluster_name,
                               api_url=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.get_by_name_and_cluster = AsyncMock(return_value=ns)
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    booking = await use_case.execute(
        AsyncMock(), 240, user_id="u1", namespace_name="team-a-dev", cluster_name="prod-cluster"
    )

    ns_repo.get_by_name_and_cluster.assert_awaited_once_with(
        ns_repo.get_by_name_and_cluster.call_args.args[0], "team-a-dev", "prod-cluster"
    )
    # Resolved to the matching id and locked that specific namespace.
    ns_repo.lock_for_allocation.assert_awaited_once()
    assert ns_repo.lock_for_allocation.call_args.args[1] == ns.id
    assert booking.namespace_id == ns.id


@pytest.mark.asyncio
async def test_use_case_unknown_pair_raises_unavailable():
    repo = MagicMock()
    ns_repo = MagicMock()
    ns_repo.get_by_name_and_cluster = AsyncMock(return_value=None)
    ns_repo.lock_for_allocation = AsyncMock()

    use_case = BookNamespaceUseCase(repo, ns_repo)
    with pytest.raises(NamespaceUnavailableError):
        await use_case.execute(AsyncMock(), 240, user_id="u1",
                               namespace_name="nope", cluster_name="prod-cluster")
    ns_repo.lock_for_allocation.assert_not_called()


@pytest.mark.asyncio
async def test_use_case_namespace_id_takes_precedence_over_pair():
    """An explicit namespace_id wins — the pair is never resolved."""
    ns_id = uuid4()
    ns_model = SimpleNamespace(id=ns_id, name="x", cluster_name="c", api_url=None, is_active=True)
    repo = MagicMock()
    repo.create = AsyncMock(side_effect=lambda session, booking: booking)
    ns_repo = MagicMock()
    ns_repo.get_by_name_and_cluster = AsyncMock()
    ns_repo.lock_for_allocation = AsyncMock(return_value=ns_model)
    ns_repo.is_held = AsyncMock(return_value=False)

    use_case = BookNamespaceUseCase(repo, ns_repo)
    await use_case.execute(AsyncMock(), 240, user_id="u1", namespace_id=ns_id,
                           namespace_name="ignored", cluster_name="ignored")

    ns_repo.get_by_name_and_cluster.assert_not_called()
    assert ns_repo.lock_for_allocation.call_args.args[1] == ns_id


# ── API: POST /api/bookings by pair ───────────────────────────────────────────
@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_order_namespace_by_pair_returns_201(client):
    ns_id = uuid4()
    booking = _ns_booking(ns_id)
    with patch("app.presentation.routes.api_bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "NAMESPACE", "ttl_minutes": 240,
            "namespace_name": "team-a-dev", "cluster_name": "prod-cluster",
        })

    assert resp.status_code == 201
    assert resp.json()["namespace"] == "team-a-dev"
    # The pair was forwarded to the use case.
    kwargs = mock_uc.execute.call_args.kwargs
    assert kwargs["namespace_name"] == "team-a-dev"
    assert kwargs["cluster_name"] == "prod-cluster"


def test_api_order_namespace_one_of_pair_returns_400(client):
    with patch("app.presentation.routes.api_bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock()
        resp = client.post("/api/bookings", json={
            "resource_type": "NAMESPACE", "ttl_minutes": 240, "namespace_name": "team-a-dev",
        })
    assert resp.status_code == 400
    mock_uc.execute.assert_not_called()


def test_api_order_namespace_unknown_pair_returns_409(client):
    with patch("app.presentation.routes.api_bookings._book_namespace_use_case") as mock_uc:
        mock_uc.execute = AsyncMock(side_effect=NamespaceUnavailableError("No namespace 'nope' on cluster 'prod-cluster'"))
        resp = client.post("/api/bookings", json={
            "resource_type": "NAMESPACE", "ttl_minutes": 240,
            "namespace_name": "nope", "cluster_name": "prod-cluster",
        })
    assert resp.status_code == 409
