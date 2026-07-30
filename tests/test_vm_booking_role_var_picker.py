"""Tests for #357 (v0.12.0 F-4): Ansible role and variable input on the VM booking form.

Covers: the shared `resolve_config_roles`/`validate_var_names` helpers (now used by both
`api_bookings.py` and `bookings.py`), the HTML form's local `_parse_vars_yaml`, and the new
`roles`/`vars_yaml` fields on `POST /bookings`.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.exceptions import RoleNotFoundError
from app.domain.validation import validate_var_names


# ── validate_var_names ────────────────────────────────────────────────────────

def test_validate_var_names_accepts_valid_keys():
    validate_var_names({"my_var": 1, "CamelCase": 2, "_private": 3, "x1": 4})


def test_validate_var_names_rejects_hyphenated_key():
    with pytest.raises(ValueError, match="my-var"):
        validate_var_names({"my-var": "value"})


def test_validate_var_names_rejects_digit_leading_key():
    with pytest.raises(ValueError):
        validate_var_names({"1bad": "value"})


# ── resolve_config_roles ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_config_roles_returns_snapshot():
    from app.application.use_cases._roles import resolve_config_roles

    role_repo = MagicMock()
    role_repo.get_by_name = AsyncMock(return_value=SimpleNamespace(
        name="docker-machine", ansible_role="docker_machine",
        default_vars={"a": 1}, secret_vars={},
    ))

    result = await resolve_config_roles(None, role_repo, ["docker-machine"])

    assert result == [{
        "name": "docker-machine", "ansible_role": "docker_machine",
        "vars": {"a": 1}, "secret_vars": {},
    }]


@pytest.mark.asyncio
async def test_resolve_config_roles_empty_list_returns_empty():
    from app.application.use_cases._roles import resolve_config_roles

    role_repo = MagicMock()
    role_repo.get_by_name = AsyncMock()

    result = await resolve_config_roles(None, role_repo, [])

    assert result == []
    role_repo.get_by_name.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_config_roles_unknown_name_raises():
    from app.application.use_cases._roles import resolve_config_roles

    role_repo = MagicMock()
    role_repo.get_by_name = AsyncMock(return_value=None)

    with pytest.raises(RoleNotFoundError, match="nope"):
        await resolve_config_roles(None, role_repo, ["nope"])


# ── _parse_vars_yaml ───────────────────────────────────────────────────────────

def test_parse_vars_yaml_blank_returns_empty_dict():
    from app.presentation.routes.bookings import _parse_vars_yaml

    assert _parse_vars_yaml("") == {}
    assert _parse_vars_yaml("   \n  ") == {}


def test_parse_vars_yaml_valid_mapping():
    from app.presentation.routes.bookings import _parse_vars_yaml

    assert _parse_vars_yaml("env: staging\nreplicas: 2") == {"env": "staging", "replicas": 2}


def test_parse_vars_yaml_invalid_yaml_raises():
    from app.presentation.routes.bookings import _parse_vars_yaml

    with pytest.raises(ValueError, match="valid YAML"):
        _parse_vars_yaml("env: [unterminated")


def test_parse_vars_yaml_non_mapping_raises():
    from app.presentation.routes.bookings import _parse_vars_yaml

    with pytest.raises(ValueError, match="mapping"):
        _parse_vars_yaml("- just\n- a\n- list")


def test_parse_vars_yaml_invalid_var_name_raises():
    from app.presentation.routes.bookings import _parse_vars_yaml

    with pytest.raises(ValueError, match="bad-key"):
        _parse_vars_yaml("bad-key: value")


# ── POST /bookings — roles + vars ─────────────────────────────────────────────

def _vm_booking() -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    session_mock = AsyncMock()
    fake_user = make_fake_user()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client():
    """The role picker is admin-only as of #377 (F-6) — tests that exercise `roles` need an
    admin `current_user`; non-admin `roles` behavior is covered in
    test_admin_gate_role_picker.py."""
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    session_mock = AsyncMock()
    fake_user = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_booking_with_roles_and_vars_forwards_to_use_case(admin_client):
    client = admin_client
    booking = _vm_booking()
    with patch("app.presentation.routes.bookings._use_case") as mock_uc, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role_repo:
        mock_uc.execute = AsyncMock(return_value=booking)
        mock_role_repo.get_by_name = AsyncMock(return_value=SimpleNamespace(
            name="docker-machine", ansible_role="docker_machine",
            default_vars={}, secret_vars={},
        ))
        resp = client.post("/bookings", data={
            "resource_type": "VM", "ttl_minutes": "240",
            "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
            "roles": ["docker-machine"], "vars_yaml": "env: staging",
        })

    assert resp.status_code == 201
    kwargs = mock_uc.execute.call_args.kwargs
    assert kwargs["config_roles"] == [{
        "name": "docker-machine", "ansible_role": "docker_machine",
        "vars": {}, "secret_vars": {},
    }]
    assert kwargs["extra_vars"] == {"env": "staging"}


def test_post_booking_no_roles_no_vars_behaves_as_before(client):
    booking = _vm_booking()
    with patch("app.presentation.routes.bookings._use_case") as mock_uc:
        mock_uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/bookings", data={
            "resource_type": "VM", "ttl_minutes": "240",
            "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
        })

    assert resp.status_code == 201
    kwargs = mock_uc.execute.call_args.kwargs
    assert kwargs["config_roles"] == []
    assert kwargs["extra_vars"] == {}


def test_post_booking_unknown_role_shows_error_banner(admin_client):
    client = admin_client
    with patch("app.presentation.routes.bookings._use_case") as mock_uc, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role_repo, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm:
        mock_role_repo.get_by_name = AsyncMock(return_value=None)
        mock_role_repo.list_active = AsyncMock(return_value=[])
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        resp = client.post("/bookings", data={
            "resource_type": "VM", "ttl_minutes": "240",
            "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
            "roles": ["nope"],
        })

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#booking-form-area"
    assert "no role named" in resp.text and "nope" in resp.text
    mock_uc.execute.assert_not_called()


def test_post_booking_invalid_vars_yaml_shows_error_banner_and_preserves_input(client):
    with patch("app.presentation.routes.bookings._use_case") as mock_uc, \
         patch("app.presentation.routes.bookings._image_repo") as mock_img, \
         patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
         patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
         patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
         patch("app.presentation.routes.bookings._role_repo") as mock_role_repo:
        mock_img.list_active = AsyncMock(return_value=[])
        mock_hw.list_active = AsyncMock(return_value=[])
        mock_ns.list_available = AsyncMock(return_value=[])
        mock_svm.list_available = AsyncMock(return_value=[])
        mock_role_repo.list_active = AsyncMock(return_value=[])
        resp = client.post("/bookings", data={
            "resource_type": "VM", "ttl_minutes": "240",
            "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
            "vars_yaml": "bad-key: value",
        })

    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#booking-form-area"
    assert "bad-key" in resp.text
    assert "bad-key: value" in resp.text  # user's input preserved in the re-rendered textarea
    mock_uc.execute.assert_not_called()
