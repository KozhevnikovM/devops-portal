"""Regression test for #140 — PATCH /api/hardware/{id} must persist disk updates.

Before the fix, HWConfigUpdate declared the disk field as `disk_mb` while the column is
`disk_mb`. A `{"disk_mb": ...}` body was dropped as an unknown field (never reached the repo),
and a `{"disk_mb": ...}` body set a stray non-column attribute that never persisted. Either
way disk edits silently no-opped. After the fix the schema field is `disk_mb`.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.entities import HWConfig


def _client():
    from app.main import app
    from app.infrastructure.auth import require_admin
    from tests.conftest import make_fake_admin

    app.dependency_overrides[require_admin] = lambda: make_fake_admin()
    return app


def _hw(disk_mb: int) -> HWConfig:
    return HWConfig(
        id=uuid4(),
        name="medium",
        cpus=2,
        memory_mb=4096,
        disk_mb=disk_mb,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def test_patch_hardware_persists_disk_mb():
    app = _client()
    hw_id = uuid4()
    updated = _hw(51200)
    try:
        with patch("app.presentation.routes.api._hw_config_repo") as mock_repo:
            mock_repo.update = AsyncMock(return_value=updated)
            resp = TestClient(app).patch(f"/api/hardware/{hw_id}", json={"disk_mb": 51200})

        assert resp.status_code == 200
        # The disk value reached the persistence layer under the column name `disk_mb`.
        mock_repo.update.assert_awaited_once()
        _, _, fields = mock_repo.update.await_args.args
        assert fields == {"disk_mb": 51200}
        assert resp.json()["disk_mb"] == 51200
    finally:
        app.dependency_overrides.clear()
