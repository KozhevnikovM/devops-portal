import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.domain.entities import HWConfig


def _make_hw(**kwargs) -> HWConfig:
    return HWConfig(
        id=kwargs.get("id", uuid4()),
        name=kwargs.get("name", "medium"),
        cpus=kwargs.get("cpus", 2),
        memory_mb=kwargs.get("memory_mb", 4096),
        hdd_mb=kwargs.get("hdd_mb", 51200),
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_admin] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_hw_config_converts_gb_to_mb(client):
    """POST with memory_gb=4, hdd_gb=50 stores memory_mb=4096, hdd_mb=51200."""
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_repo:
        mock_repo.create = AsyncMock()
        mock_repo.list_all = AsyncMock(return_value=[])

        resp = client.post("/admin/catalog/hardware", data={
            "name": "medium",
            "cpus": "2",
            "memory_gb": "4",
            "hdd_gb": "50",
        })

    assert resp.status_code == 200
    mock_repo.create.assert_called_once()
    _, _, _, memory_mb, hdd_mb = mock_repo.create.call_args.args
    assert memory_mb == 4096
    assert hdd_mb == 51200


def test_update_hw_config_converts_gb_to_mb(client):
    """PATCH with memory_gb=8, hdd_gb=100 stores memory_mb=8192, hdd_mb=102400."""
    hw_id = uuid4()
    with patch("app.presentation.routes.admin._hw_config_repo") as mock_repo:
        mock_repo.update = AsyncMock()
        mock_repo.list_all = AsyncMock(return_value=[])

        resp = client.patch(f"/admin/catalog/hardware/{hw_id}", data={
            "name": "large",
            "cpus": "4",
            "memory_gb": "8",
            "hdd_gb": "100",
        })

    assert resp.status_code == 200
    mock_repo.update.assert_called_once()
    update_dict = mock_repo.update.call_args.args[2]
    assert update_dict["memory_mb"] == 8192
    assert update_dict["hdd_mb"] == 102400
