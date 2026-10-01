import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.pagination import KeysetPage
from app.domain.enums import BookingStatus


def _make_booking(user_id="user-a") -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(),
        user_id=user_id,
        status=BookingStatus.READY,
        ttl_minutes=60,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        image_id=uuid4(),
        image_name="Ubuntu 22.04",
        hw_config_id=uuid4(),
        hw_config_name="medium",
    )


@pytest.fixture
def setup():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    fake_user = make_fake_admin()
    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


def test_default_filter_lists_mine(setup):
    """GET / without filter param lists the user's own bookings (default: mine)."""
    client, fake_user = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role:
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])
        mock_repo.list_page = AsyncMock(return_value=KeysetPage(items=[]))
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])

        resp = client.get("/")

    assert resp.status_code == 200
    mock_repo.list_page.assert_called_once()
    assert mock_repo.list_page.call_args.kwargs["user_id"] == str(fake_user.id)


def test_vm_filter_tabs_use_named_book_vm_path(setup):
    """#192: the VM filter tabs target /book/vm, not the bare "/" — now its list fragment (#494).

    The bare "/?filter=…" URL isn't matched by the reverse-proxy sub_filter rules, so it 404s
    behind a /dp subpath. Rendering the tabs against /book/vm (covered by the ="/book" rule)
    keeps them working both locally and behind the proxy.
    """
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role:
        mock_repo.list_page = AsyncMock(return_value=KeysetPage(items=[]))
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])

        resp = client.get("/")

    assert resp.status_code == 200
    assert 'hx-get="/book/vm/list?filter=all"' in resp.text
    assert 'hx-get="/book/vm/list?filter=mine"' in resp.text
    # The bare-"/" filter URL (unreachable behind the /dp proxy) is gone.
    assert 'hx-get="/?filter=' not in resp.text


def test_filter_mine_lists_mine(setup):
    """GET /?filter=mine lists the user's own bookings."""
    client, fake_user = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role:
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])
        mock_repo.list_page = AsyncMock(return_value=KeysetPage(items=[]))
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])

        resp = client.get("/?filter=mine")

    assert resp.status_code == 200
    mock_repo.list_page.assert_called_once()
    assert mock_repo.list_page.call_args.kwargs["user_id"] == str(fake_user.id)


def test_filter_all_lists_everyone(setup):
    """GET /?filter=all lists everyone's bookings."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role:
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role.list_active = AsyncMock(return_value=[])
        mock_repo.list_page = AsyncMock(return_value=KeysetPage(items=[]))
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])

        resp = client.get("/?filter=all")

    assert resp.status_code == 200
    mock_repo.list_page.assert_called_once()
    assert mock_repo.list_page.call_args.kwargs["user_id"] is None
