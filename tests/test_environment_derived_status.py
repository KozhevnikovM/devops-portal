"""#434 — a partly released environment is reported FAILED (never READY) by the JSON API and the row."""
from unittest.mock import AsyncMock, patch

from app.domain.enums import BookingStatus, ResourceType
from tests.test_environment_ui import (  # noqa: F401 — `client` is a fixture
    _child,
    _env,
    client,
)


def _partly_released_env():
    return _env(children=[
        _child(rt=ResourceType.NAMESPACE, status=BookingStatus.RELEASED),
        _child(rt=ResourceType.VM, status=BookingStatus.READY),
    ])


def test_json_status_is_failed_for_released_plus_ready(client):  # noqa: F811
    env = _partly_released_env()
    with patch("app.presentation.routes.api_environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/api/v1/environments/{env.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"


def test_row_shows_failed_and_keeps_release_for_released_plus_ready(client):  # noqa: F811
    env = _partly_released_env()
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/environments/{env.id}/row")
    assert resp.status_code == 200
    assert "status-FAILED" in resp.text
    assert f'hx-delete="/environments/{env.id}"' in resp.text


def test_json_status_is_ready_when_every_child_ready(client):  # noqa: F811
    env = _env(children=[_child(status=BookingStatus.READY), _child(status=BookingStatus.READY)])
    with patch("app.presentation.routes.api_environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        resp = client.get(f"/api/v1/environments/{env.id}")
    assert resp.json()["status"] == "READY"
