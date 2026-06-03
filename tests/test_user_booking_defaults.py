import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.domain.entities import HWConfig, User, VMImage


def _make_image(name="Ubuntu 22.04") -> VMImage:
    return VMImage(
        id=uuid4(),
        name=name,
        vapp_template_id="tmpl-1",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _make_hw(name="medium") -> HWConfig:
    return HWConfig(
        id=uuid4(),
        name=name,
        cpus=2,
        memory_mb=4096,
        hdd_mb=51200,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


def _make_user(image_id=None, hw_config_id=None) -> User:
    return User(
        id=uuid4(),
        username="test-user",
        password_hash="",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        default_image_id=image_id,
        default_hw_config_id=hw_config_id,
    )


@pytest.fixture
def setup():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    fake_user = _make_user()
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


def test_patch_defaults_saves_both_fields(setup):
    """PATCH /profile/defaults persists the chosen image and hw config."""
    client, fake_user = setup
    image = _make_image()
    hw = _make_hw()

    with patch("app.presentation.routes.auth._user_repo") as mock_user, \
         patch("app.presentation.routes.auth._image_repo") as mock_img, \
         patch("app.presentation.routes.auth._hw_config_repo") as mock_hw:
        mock_img.list_active = AsyncMock(return_value=[image])
        mock_hw.list_active = AsyncMock(return_value=[hw])
        mock_user.set_defaults = AsyncMock()
        mock_user.list_api_keys = AsyncMock(return_value=[])
        mock_user.get = AsyncMock(
            return_value=_make_user(image_id=image.id, hw_config_id=hw.id)
        )

        resp = client.patch(
            "/profile/defaults",
            data={"default_image_id": str(image.id), "default_hw_config_id": str(hw.id)},
        )

    assert resp.status_code == 200
    mock_user.set_defaults.assert_called_once_with(
        mock_user.set_defaults.call_args.args[0], fake_user.id, image.id, hw.id
    )
    assert "Booking defaults saved." in resp.text


def test_patch_defaults_clears_to_no_preference(setup):
    """Empty form values clear the saved defaults (None)."""
    client, fake_user = setup

    with patch("app.presentation.routes.auth._user_repo") as mock_user, \
         patch("app.presentation.routes.auth._image_repo") as mock_img, \
         patch("app.presentation.routes.auth._hw_config_repo") as mock_hw:
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_user.set_defaults = AsyncMock()
        mock_user.list_api_keys = AsyncMock(return_value=[])
        mock_user.get = AsyncMock(return_value=_make_user())

        resp = client.patch(
            "/profile/defaults",
            data={"default_image_id": "", "default_hw_config_id": ""},
        )

    assert resp.status_code == 200
    _, _, image_id, hw_config_id = mock_user.set_defaults.call_args.args
    assert image_id is None
    assert hw_config_id is None


def test_patch_defaults_rejects_unknown_image(setup):
    """An image id not among active images is rejected with 400."""
    client, _ = setup
    hw = _make_hw()

    with patch("app.presentation.routes.auth._user_repo") as mock_user, \
         patch("app.presentation.routes.auth._image_repo") as mock_img, \
         patch("app.presentation.routes.auth._hw_config_repo") as mock_hw:
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[hw])
        mock_user.set_defaults = AsyncMock()

        resp = client.patch(
            "/profile/defaults",
            data={"default_image_id": str(uuid4()), "default_hw_config_id": str(hw.id)},
        )

    assert resp.status_code == 400
    mock_user.set_defaults.assert_not_called()


def test_profile_get_renders_defaults_section(setup):
    """GET /profile renders the Booking defaults section with options."""
    client, _ = setup
    image = _make_image()
    hw = _make_hw()

    with patch("app.presentation.routes.auth._user_repo") as mock_user, \
         patch("app.presentation.routes.auth._image_repo") as mock_img, \
         patch("app.presentation.routes.auth._hw_config_repo") as mock_hw:
        mock_img.list_active = AsyncMock(return_value=[image])
        mock_hw.list_active = AsyncMock(return_value=[hw])
        mock_user.list_api_keys = AsyncMock(return_value=[])

        resp = client.get("/profile")

    assert resp.status_code == 200
    assert "Booking defaults" in resp.text
    assert image.name in resp.text


def test_booking_form_preselects_user_defaults(setup):
    """Booking form marks the user's default image and hw config as selected."""
    client = TestClient
    image = _make_image()
    hw = _make_hw()
    fake_user = _make_user(image_id=image.id, hw_config_id=hw.id)

    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    app.dependency_overrides[require_user] = lambda: fake_user
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    try:
        with patch("app.presentation.routes.bookings._repo") as mock_repo, \
             patch("app.presentation.routes.bookings._image_repo") as mock_img, \
             patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
             patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
             patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
            mock_repo.list_by_user = AsyncMock(return_value=[])
            mock_img.list_active = AsyncMock(return_value=[image])
            mock_hw.list_active = AsyncMock(return_value=[hw])
            mock_ns.list_available = AsyncMock(return_value=[])
            mock_svm.list_available = AsyncMock(return_value=[])

            resp = TestClient(app).get("/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    # Both the default image and hw config options should carry selected.
    assert f'value="{image.id}" selected' in resp.text
    assert f'value="{hw.id}" selected' in resp.text


def test_booking_form_no_default_has_no_selected(setup):
    """With no defaults set, no image/hw option is pre-selected."""
    client, _ = setup
    image = _make_image()
    hw = _make_hw()

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[image])
        mock_hw.list_active = AsyncMock(return_value=[hw])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])

        resp = client.get("/")

    assert resp.status_code == 200
    assert f'value="{image.id}" selected' not in resp.text
    assert f'value="{hw.id}" selected' not in resp.text
