"""Regression tests for #144 — admin error fragments escape user input.

Before the fix, a duplicate name/username was interpolated raw into an HTMLResponse, so
`<img src=x onerror=...>` injected live markup. After the fix the value is markupsafe-escaped.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

XSS = '<script>alert(1)</script>'


@pytest.fixture
def client():
    from app.infrastructure.auth import require_admin
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    from tests.conftest import make_fake_admin

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_admin] = lambda: make_fake_admin()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_duplicate_image_name_is_escaped(client):
    with patch("app.presentation.routes.admin._image_repo") as mock_img:
        mock_img.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/catalog/images",
            data={"name": XSS, "vapp_template_id": "tpl"},
        )

    assert resp.status_code == 200
    assert XSS not in resp.text                 # raw markup must not appear
    assert "&lt;script&gt;" in resp.text        # escaped form is present


def test_duplicate_username_is_escaped(client):
    with patch("app.presentation.routes.auth._user_repo") as mock_repo:
        mock_repo.create = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        resp = client.post(
            "/admin/users",
            data={"username": XSS, "password": "pw", "role": "user"},
        )

    assert resp.status_code == 200
    assert XSS not in resp.text
    assert "&lt;script&gt;" in resp.text
