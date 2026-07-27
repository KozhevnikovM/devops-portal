"""Tests for #346: filter bookings and environments by label."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def booking_client():
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


# ── HTMX bookings page ──────────────────────────────────────────────────────────

def test_bookings_page_forwards_label_to_list_by_user(booking_client):
    client, _ = booking_client
    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings._image_repo") as mock_img,
        patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.bookings._namespace_repo") as mock_ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm,
    ):
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])

        resp = client.get("/?label=perf")

    assert resp.status_code == 200
    assert mock_repo.list_by_user.call_args.kwargs["label"] == "perf"


def test_bookings_page_no_label_defaults_none(booking_client):
    client, _ = booking_client
    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings._image_repo") as mock_img,
        patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.bookings._namespace_repo") as mock_ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm,
    ):
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])

        resp = client.get("/")

    assert resp.status_code == 200
    assert mock_repo.list_by_user.call_args.kwargs["label"] is None


def test_bookings_page_filter_all_forwards_label(booking_client):
    client, _ = booking_client
    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings._image_repo") as mock_img,
        patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.bookings._namespace_repo") as mock_ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm,
    ):
        mock_repo.list_all = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])

        resp = client.get("/?filter=all&label=dev")

    assert resp.status_code == 200
    assert mock_repo.list_all.call_args.kwargs["label"] == "dev"


def test_mine_all_toggle_preserves_label_filter(booking_client):
    """Switching Mine/All/Show-released must not drop an active label filter."""
    client, _ = booking_client
    with (
        patch("app.presentation.routes.bookings._repo") as mock_repo,
        patch("app.presentation.routes.bookings._image_repo") as mock_img,
        patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw,
        patch("app.presentation.routes.bookings._namespace_repo") as mock_ns,
        patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm,
    ):
        mock_repo.list_by_user = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])

        resp = client.get("/?label=perf")

    assert "label=perf" in resp.text


# ── JSON API bookings ────────────────────────────────────────────────────────────

def test_api_bookings_forwards_label(booking_client):
    client, _ = booking_client
    with patch("app.presentation.routes.api_bookings._repo") as mock_repo:
        mock_repo.list_all = AsyncMock(return_value=[])
        resp = client.get("/api/bookings?label=perf")

    assert resp.status_code == 200
    assert mock_repo.list_all.call_args.kwargs["label"] == "perf"


# ── HTMX environments page ───────────────────────────────────────────────────────

def test_environments_page_forwards_label(booking_client):
    client, _ = booking_client
    with (
        patch("app.presentation.routes.environments._env_repo") as mock_env,
        patch("app.presentation.routes.environments._blueprint_repo") as mock_bp,
        patch("app.presentation.routes.environments._namespace_repo") as mock_ns,
    ):
        mock_env.list_by_user = AsyncMock(return_value=[])
        mock_bp.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_ns.list_held_standalone_by_user = AsyncMock(return_value=[])

        resp = client.get("/environments?label=dev-stack")

    assert resp.status_code == 200
    assert mock_env.list_by_user.call_args.kwargs["label"] == "dev-stack"


def test_environments_page_toggle_preserves_label_filter(booking_client):
    client, _ = booking_client
    with (
        patch("app.presentation.routes.environments._env_repo") as mock_env,
        patch("app.presentation.routes.environments._blueprint_repo") as mock_bp,
        patch("app.presentation.routes.environments._namespace_repo") as mock_ns,
    ):
        mock_env.list_by_user = AsyncMock(return_value=[])
        mock_bp.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_ns.list_held_standalone_by_user = AsyncMock(return_value=[])

        resp = client.get("/environments?label=dev-stack")

    assert "label=dev-stack" in resp.text


# ── JSON API environments ───────────────────────────────────────────────────────

def test_api_environments_forwards_label(booking_client):
    client, _ = booking_client
    with patch("app.presentation.routes.api_environments._env_repo") as mock_env:
        mock_env.list_all = AsyncMock(return_value=[])
        resp = client.get("/api/environments?label=dev-stack")

    assert resp.status_code == 200
    assert mock_env.list_all.call_args.kwargs["label"] == "dev-stack"


# ── Repository: ILIKE filter on the WHERE clause ─────────────────────────────────

@pytest.mark.asyncio
async def test_booking_repo_label_filter_applies_ilike():
    from app.infrastructure.repositories.booking_repo import BookingRepository

    repo = BookingRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session, label="perf")
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # Default (non-Postgres) dialect renders .ilike() as LOWER(col) LIKE LOWER(pattern);
    # Postgres renders it as native ILIKE. Either way, this proves case-insensitive matching.
    assert "LOWER(BOOKINGS.LABEL) LIKE LOWER" in compiled.upper()
    assert "%PERF%" in compiled.upper()


@pytest.mark.asyncio
async def test_booking_repo_no_label_omits_filter():
    from app.infrastructure.repositories.booking_repo import BookingRepository

    repo = BookingRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session)
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIKE" not in compiled.upper()


@pytest.mark.asyncio
async def test_booking_repo_blank_label_is_noop():
    from app.infrastructure.repositories.booking_repo import BookingRepository

    repo = BookingRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session, label="   ")
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIKE" not in compiled.upper()


@pytest.mark.asyncio
async def test_environment_repo_label_filter_applies_ilike_on_name():
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository

    repo = EnvironmentRepository()
    session = AsyncMock()
    result = AsyncMock()
    result.all = lambda: []
    session.execute = AsyncMock(return_value=result)

    await repo.list_all(session, label="dev")
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LOWER(ENVIRONMENTS.NAME) LIKE LOWER" in compiled.upper()
    assert "%DEV%" in compiled.upper()
