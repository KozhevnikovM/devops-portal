import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


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


# ── Route: default hides released, ?show_released=1 includes them ──────────────

def test_default_excludes_released(setup):
    """GET / (default) calls list_by_user with include_released=False."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])

        resp = client.get("/")

    assert resp.status_code == 200
    assert mock_repo.list_by_user.call_args.kwargs["include_released"] is False


def test_show_released_includes_them(setup):
    """GET /?show_released=1 calls list_by_user with include_released=True."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])

        resp = client.get("/?show_released=1")

    assert resp.status_code == 200
    assert mock_repo.list_by_user.call_args.kwargs["include_released"] is True


def test_filter_all_default_excludes_released(setup):
    """GET /?filter=all (default) calls list_all with include_released=False."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_repo.list_all = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])

        resp = client.get("/?filter=all")

    assert resp.status_code == 200
    assert mock_repo.list_all.call_args.kwargs["include_released"] is False


def test_filter_all_show_released_includes_them(setup):
    """GET /?filter=all&show_released=1 calls list_all with include_released=True."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_repo.list_all = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])

        resp = client.get("/?filter=all&show_released=1")

    assert resp.status_code == 200
    assert mock_repo.list_all.call_args.kwargs["include_released"] is True


def test_toggle_button_preserves_owner_filter(setup):
    """The released toggle link keeps the active owner filter (filter=all)."""
    client, _ = setup

    with patch("app.presentation.routes.bookings._repo") as mock_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns:
        mock_repo.list_all = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])

        resp = client.get("/?filter=all")

    assert resp.status_code == 200
    # While released is hidden, the toggle turns it on but keeps filter=all.
    assert "/?filter=all&show_released=1" in resp.text
    assert "Show released" in resp.text


# ── Repository: include_released flag drives the WHERE clause ──────────────────

@pytest.mark.asyncio
async def test_repo_list_all_excludes_released_by_default():
    from app.infrastructure.repositories.booking_repo import BookingRepository
    from app.domain.enums import BookingStatus

    repo = BookingRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert BookingStatus.RELEASED.value in compiled
    assert "status !=" in compiled.replace("status <>", "status !=")


@pytest.mark.asyncio
async def test_repo_list_all_includes_released_when_requested():
    from app.infrastructure.repositories.booking_repo import BookingRepository
    from app.domain.enums import BookingStatus

    repo = BookingRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session, include_released=True)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert BookingStatus.RELEASED.value not in compiled
