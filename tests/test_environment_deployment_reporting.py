"""Complete reporting boundary, JSON contract, and scoped post-commit invalidation (#535)."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.update_environment_deployment import (
    UpdateEnvironmentDeploymentUseCase,
)
from app.domain.deployment import DeploymentReport, DeploymentStatus
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import EnvironmentError, EnvironmentNotFoundError
from app.infrastructure.events import Routing, apublish_environment_changed
from app.infrastructure.repositories.environment_repo import EnvironmentRepository

NOW = datetime.now(UTC)
PAYLOAD = {
    "deployment_ref": " feature/<ref> ",
    "deployment_commit": "aB" * 20,
    "deployment_status": "DEPLOYING",
    "deployment_frontend_url": "HTTPS://Frontend.Example:443/path?q=%2F#part",
    "deployment_admin_url": "https://admin.example/exact?x=1&y=2#fragment",
}


def _environment(status=BookingStatus.READY):
    owner = User(uuid4(), "owner", "", "user", True, NOW)
    child = Booking(
        id=uuid4(),
        user_id=str(owner.id),
        status=status,
        resource_type=ResourceType.NAMESPACE,
        ttl_minutes=60,
        expires_at=NOW,
        created_at=NOW,
    )
    return owner, Environment(
        uuid4(), "stack", "blueprint", str(owner.id), 60, NOW, NOW, [child]
    )


@pytest.mark.parametrize("commit", ["a" * 40, "B" * 64])
def test_report_keeps_original_strings(commit):
    report = DeploymentReport(**(PAYLOAD | {"deployment_commit": commit}))
    assert report.deployment_status == DeploymentStatus.DEPLOYING
    assert report.deployment_ref == PAYLOAD["deployment_ref"]
    assert report.deployment_frontend_url == PAYLOAD["deployment_frontend_url"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deployment_ref", " "),
        ("deployment_ref", "a" * 513),
        ("deployment_ref", "ref\n"),
        ("deployment_ref", "ref\x7f"),
        ("deployment_commit", "a" * 7),
        ("deployment_commit", "g" * 40),
        ("deployment_commit", "a" * 41),
        ("deployment_status", "READY"),
        ("deployment_frontend_url", "javascript:alert(1)"),
        ("deployment_frontend_url", "https://user:password@example.test/"),
        ("deployment_frontend_url", "https://@example.test/"),
        ("deployment_frontend_url", "https://example.test:broken/"),
        ("deployment_frontend_url", "https://example.test/\n"),
        ("deployment_frontend_url", "https://example.test/ has-space"),
        ("deployment_frontend_url", "https://"),
        ("deployment_frontend_url", "/relative"),
        ("deployment_frontend_url", "https://example.test/" + "a" * 2048),
    ],
)
def test_invalid_domain_report(field, value):
    with pytest.raises(ValueError):
        DeploymentReport(**(PAYLOAD | {field: value}))


class MemoryRepo:
    def __init__(self, env):
        self.env = env
        self.writes = 0

    async def get_for_update(self, session, environment_id):
        if environment_id != self.env.id:
            raise EnvironmentNotFoundError("missing")
        return self.env

    async def update_deployment(self, session, environment_id, report):
        if all(b.status == BookingStatus.RELEASED for b in self.env.bookings):
            raise EnvironmentError("released")
        self.writes += 1
        self.env = replace(self.env, **asdict(report), deployment_updated_at=NOW)
        return self.env

    async def get(self, session, environment_id):
        return await self.get_for_update(session, environment_id)


@pytest.fixture
def api():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    owner, env = _environment()
    repo = MemoryRepo(env)
    state = {"user": owner}
    session = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session
    app.dependency_overrides[require_user] = lambda: state["user"]
    with (
        patch(
            "app.presentation.routes.api_environments._update_deployment_use_case",
            UpdateEnvironmentDeploymentUseCase(repo),
        ),
        patch("app.presentation.routes.api_environments._env_repo", repo),
    ):
        yield TestClient(app), repo, state, session
    app.dependency_overrides.clear()


@pytest.mark.parametrize("prefix", ["/api/v1", "/api"])
def test_both_mounts_report_and_preserve_lease(api, prefix):
    client, repo, _, _ = api
    before = repo.env
    response = client.patch(
        f"{prefix}/environments/{before.id}/deployment-info", json=PAYLOAD
    )
    assert response.status_code == 200
    assert {key: response.json()[key] for key in PAYLOAD} == PAYLOAD
    assert response.json()["deployment_updated_at"] == NOW.isoformat()
    assert response.json()["status"] == "READY"
    assert repo.env.expires_at == before.expires_at
    assert repo.env.bookings == before.bookings
    assert client.get(f"{prefix}/environments/{before.id}").json() == response.json()


@pytest.mark.parametrize("field", list(PAYLOAD))
@pytest.mark.parametrize("mode", ["omit", "null", "wrong-type"])
def test_api_rejects_incomplete_or_coerced_fields(api, field, mode):
    client, repo, _, _ = api
    payload = dict(PAYLOAD)
    if mode == "omit":
        del payload[field]
    else:
        payload[field] = None if mode == "null" else 42
    response = client.patch(
        f"/api/v1/environments/{repo.env.id}/deployment-info", json=payload
    )
    assert response.status_code == 422
    assert repo.writes == 0
    assert repo.env.deployment_updated_at is None


def test_api_rejects_unknown_timestamp(api):
    client, repo, _, _ = api
    response = client.patch(
        f"/api/v1/environments/{repo.env.id}/deployment-info",
        json=PAYLOAD | {"deployment_updated_at": NOW.isoformat()},
    )
    assert response.status_code == 422
    assert repo.writes == 0


@pytest.mark.parametrize("role", ["user", "dispatcher", "admin"])
def test_identity_authorization(api, role):
    client, repo, state, _ = api
    actor = User(uuid4(), "ci", "", role, True, NOW)
    state["user"] = actor
    if role == "dispatcher":
        repo.env.created_by = str(actor.id)
    elif role == "user":
        repo.env.user_id = str(actor.id)
    assert (
        client.patch(
            f"/api/v1/environments/{repo.env.id}/deployment-info", json=PAYLOAD
        ).status_code
        == 200
    )


def test_authorization_precedes_released_conflict(api):
    client, repo, state, session = api
    repo.env.bookings[0].status = BookingStatus.RELEASED
    owner = state["user"]
    state["user"] = replace(owner, id=uuid4())
    path = f"/api/v1/environments/{repo.env.id}/deployment-info"
    assert client.patch(path, json=PAYLOAD).status_code == 403
    assert repo.writes == 0
    session.rollback.assert_awaited()
    state["user"] = owner
    assert client.patch(path, json=PAYLOAD).status_code == 409
    assert (
        client.patch(
            f"/api/v1/environments/{uuid4()}/deployment-info", json=PAYLOAD
        ).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_repository_captures_response_before_commit_and_publishes_after():
    _, env = _environment()
    repo = EnvironmentRepository()
    report = DeploymentReport(**PAYLOAD)
    model = MagicMock()
    session = AsyncMock()
    session.get.return_value = model
    repo.get_for_update = AsyncMock(return_value=env)
    repo.get = AsyncMock(return_value=env)
    order = []

    async def commit():
        order.append("commit")
        # A later request may overwrite ORM state after the lock is released.
        model.deployment_ref = "newer-report"

    session.commit.side_effect = commit
    with patch(
        "app.infrastructure.repositories.environment_repo.apublish_environment_changed",
        new=AsyncMock(side_effect=lambda **_: order.append("publish")),
    ):
        result = await repo.update_deployment(session, env.id, report)
    assert order == ["commit", "publish"]
    assert result.deployment_ref == PAYLOAD["deployment_ref"]
    assert result.deployment_updated_at.tzinfo is UTC
    assert result.deployment_admin_url == PAYLOAD["deployment_admin_url"]


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_without_notification():
    _, env = _environment()
    repo = EnvironmentRepository()
    session = AsyncMock()
    session.get.return_value = MagicMock()
    session.commit.side_effect = RuntimeError("database failure")
    repo.get_for_update = AsyncMock(return_value=env)
    repo.get = AsyncMock(return_value=env)
    with patch(
        "app.infrastructure.repositories.environment_repo.apublish_environment_changed",
        new=AsyncMock(),
    ) as publish:
        with pytest.raises(RuntimeError, match="database failure"):
            await repo.update_deployment(session, env.id, DeploymentReport(**PAYLOAD))
        publish.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_environment_only_publisher_routes_owner_creator_admin_and_swallows_transport_failure():
    import json

    from app.infrastructure.events import ADMIN_CHANNEL, user_channel

    env_id = uuid4()
    redis = MagicMock()
    pipe = redis.pipeline.return_value
    pipe.execute = AsyncMock(side_effect=RuntimeError("offline"))
    with patch("app.infrastructure.events.get_async_redis", return_value=redis):
        await apublish_environment_changed(
            environment_id=env_id, routing=Routing("owner", "creator")
        )
    assert [call.args[0] for call in pipe.publish.call_args_list] == [
        user_channel("owner"),
        user_channel("creator"),
        ADMIN_CHANNEL,
    ]
    payload = json.loads(pipe.publish.call_args.args[1])
    assert payload["booking_id"] is None
    assert payload["environment_id"] == str(env_id)
    assert payload["kind"] == "lifecycle"
