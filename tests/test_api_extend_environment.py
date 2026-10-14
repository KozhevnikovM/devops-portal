"""PUT /api/v1/environments/{id}/extend (and the legacy /api mount) — #532.

Drives the real ExtendEnvironmentUseCase over an in-memory repository that applies the extension
the way the database repository does (one deadline for the environment and every child), so the
response, a subsequent GET and the children's leases are all checked through the API.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.extend_environment import ExtendEnvironmentUseCase
from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import EnvironmentNotFoundError
from app.domain.lease import Lease

_OWNER = UUID(int=7)
_T = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class _MemoryEnvRepo:
    def __init__(self, env: Environment) -> None:
        self.env = env

    async def get(self, session, environment_id):
        if environment_id != self.env.id:
            raise EnvironmentNotFoundError("nope")
        return self.env

    async def extend_lease(self, session, environment_id, extend_minutes, actor_id):
        new = Lease(self.env.ttl_minutes, self.env.expires_at).extended_within_term_by(
            extend_minutes
        )
        children = [
            replace(b, ttl_minutes=new.ttl_minutes, expires_at=new.expires_at)
            for b in self.env.bookings
        ]
        self.env = replace(
            self.env,
            ttl_minutes=new.ttl_minutes,
            expires_at=new.expires_at,
            bookings=children,
        )


def _child(rt, status=BookingStatus.READY) -> Booking:
    return Booking(
        id=uuid4(),
        user_id=str(_OWNER),
        status=status,
        resource_type=rt,
        ttl_minutes=60,
        expires_at=_T,
        created_at=_T,
    )


def _env(*statuses, ttl_minutes=60, expires_at=_T) -> Environment:
    types = [ResourceType.NAMESPACE, ResourceType.STATIC_VM, ResourceType.VM]
    children = [
        _child(types[i % 3], s)
        for i, s in enumerate(statuses or [BookingStatus.READY] * 3)
    ]
    return Environment(
        id=uuid4(),
        name="stack",
        blueprint_name="bp",
        user_id=str(_OWNER),
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        created_at=_T,
        bookings=children,
    )


def _user(uid=_OWNER, role="user") -> User:
    return User(
        id=uid,
        username="owner",
        password_hash="",
        role=role,
        is_active=True,
        created_at=_T,
    )


@pytest.fixture
def api():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    state = {"user": _user()}
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: state["user"]

    def setup(env, user=None):
        if user is not None:
            state["user"] = user
        repo = _MemoryEnvRepo(env)
        patches = [
            patch(
                "app.presentation.routes.api_environments._extend_use_case",
                ExtendEnvironmentUseCase(repo),
            ),
            patch("app.presentation.routes.api_environments._env_repo", repo),
        ]
        for p in patches:
            p.start()
        setup.patches.extend(patches)
        return TestClient(app), repo

    setup.patches = []
    yield setup
    for p in setup.patches:
        p.stop()
    app.dependency_overrides.clear()


@pytest.mark.parametrize("prefix", ["/api/v1", "/api"])
def test_extend_moves_stack_to_one_new_deadline(api, prefix):
    env = _env()
    client, repo = api(env)

    resp = client.put(
        f"{prefix}/environments/{env.id}/extend", json={"extend_minutes": 720}
    )

    assert resp.status_code == 200
    new_expiry = _T + timedelta(minutes=720)
    assert resp.json() == {
        "id": str(env.id),
        "status": "READY",
        "ttl_minutes": 780,
        "expires_at": new_expiry.isoformat(),
    }
    got = client.get(f"/api/v1/environments/{env.id}").json()
    assert got["ttl_minutes"] == 780
    assert got["expires_at"] == new_expiry.isoformat()
    assert {b.expires_at for b in repo.env.bookings} == {new_expiry}
    assert {b.ttl_minutes for b in repo.env.bookings} == {780}


def test_unrelated_user_403(api):
    env = _env()
    client, repo = api(env, user=_user(uid=uuid4(), role="dispatcher"))
    resp = client.put(
        f"/api/v1/environments/{env.id}/extend", json={"extend_minutes": 60}
    )
    assert resp.status_code == 403
    assert repo.env.expires_at == _T


def test_admin_may_extend(api):
    env = _env()
    client, _ = api(env, user=_user(uid=uuid4(), role="admin"))
    assert (
        client.put(
            f"/api/v1/environments/{env.id}/extend", json={"extend_minutes": 60}
        ).status_code
        == 200
    )


def test_unknown_environment_404(api):
    client, _ = api(_env())
    resp = client.put(
        f"/api/v1/environments/{uuid4()}/extend", json={"extend_minutes": 60}
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [{"extend_minutes": 0}, {"extend_minutes": -30}, {"extend_minutes": "soon"}, {}],
)
def test_invalid_increment_422(api, body):
    env = _env()
    client, repo = api(env)
    resp = client.put(f"/api/v1/environments/{env.id}/extend", json=body)
    assert resp.status_code == 422
    assert repo.env.expires_at == _T and repo.env.ttl_minutes == 60


def test_increment_reaching_sentinel_422(api):
    env = _env()
    client, repo = api(env)
    resp = client.put(
        f"/api/v1/environments/{env.id}/extend", json={"extend_minutes": 10**12}
    )
    assert resp.status_code == 422
    assert repo.env.expires_at == _T


@pytest.mark.parametrize(
    "env",
    [
        _env(ttl_minutes=0, expires_at=PERMANENT_EXPIRES_AT),
        _env(expires_at=PERMANENT_EXPIRES_AT),
        _env(BookingStatus.READY, BookingStatus.PROVISIONING),
        _env(BookingStatus.READY, BookingStatus.FAILED),
        _env(BookingStatus.RELEASED, BookingStatus.RELEASED),
    ],
    ids=["permanent", "lease-not-started", "provisioning", "failed", "released"],
)
def test_ineligible_environment_409(api, env):
    client, repo = api(env)
    before = (repo.env.ttl_minutes, repo.env.expires_at)
    resp = client.put(
        f"/api/v1/environments/{env.id}/extend", json={"extend_minutes": 60}
    )
    assert resp.status_code == 409
    assert (repo.env.ttl_minutes, repo.env.expires_at) == before


def test_openapi_lists_only_the_versioned_route():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "put" in paths["/api/v1/environments/{environment_id}/extend"]
    assert "/api/environments/{environment_id}/extend" not in paths
