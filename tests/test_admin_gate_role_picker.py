"""Tests for #377 (v0.13.0 F-6): restrict the Ansible role picker to admins.

v0.12.0 (#357) added a role/vars picker to the VM booking form, sourced from
`_role_repo.list_active(session)` and passed into the template as `roles`, with no admin
check anywhere. Fix: `_render_bookings_page`/`_render_form_error` only pass a non-empty
`roles` list when `current_user.role == "admin"` (the template's existing `{% if roles %}`
gate then hides the picker entirely for everyone else — no template change needed), and
`create_booking` ignores submitted `roles` form data for non-admins server-side (defense in
depth against a direct POST bypassing the hidden UI). `POST /api/bookings` is unchanged and
still applies roles regardless of role — see test_vm_ansible_roles.py's
`test_order_vm_with_roles_snapshots`, which already covers that with a non-admin fake user.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking
from app.domain.enums import BookingStatus


def _vm_booking() -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


def _client_as(fake_user):
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session

    session_mock = AsyncMock()
    app.dependency_overrides[get_async_session] = lambda: session_mock
    app.dependency_overrides[require_user] = lambda: fake_user
    return TestClient(app)


_A_ROLE = SimpleNamespace(
    name="docker-machine", ansible_role="docker_machine", default_vars={}, secret_vars={},
)


# ── GET / (rendered bookings page) ────────────────────────────────────────────

def test_non_admin_sees_no_roles_picker_on_bookings_page():
    from tests.conftest import make_fake_user

    client = _client_as(make_fake_user())
    try:
        with patch("app.presentation.routes.bookings._repo") as mock_repo, \
             patch("app.presentation.routes.bookings._image_repo") as mock_img, \
             patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
             patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
             patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
             patch("app.presentation.routes.bookings._role_repo") as mock_role:
            mock_repo.list_by_user = AsyncMock(return_value=[])
            mock_img.list_active = AsyncMock(return_value=[])
            mock_hw.list_active = AsyncMock(return_value=[])
            mock_ns.list_available = AsyncMock(return_value=[])
            mock_svm.list_available = AsyncMock(return_value=[])
            mock_role.list_active = AsyncMock(return_value=[_A_ROLE])

            resp = client.get("/")

            assert resp.status_code == 200
            # The repo has an active role, but a non-admin's context must not receive it.
            mock_role.list_active.assert_not_called()
    finally:
        from app.main import app
        app.dependency_overrides.clear()

    assert 'id="roles-dropdown"' not in resp.text
    assert "Ansible roles" not in resp.text
    # The role-picker JS (querySelectorAll('input[name="roles"]')) is unconditional in the
    # template, so check for the actual checkbox markup rather than the bare substring.
    assert '<input type="checkbox" name="roles"' not in resp.text


def test_admin_sees_roles_picker_on_bookings_page():
    from tests.conftest import make_fake_admin

    client = _client_as(make_fake_admin())
    try:
        with patch("app.presentation.routes.bookings._repo") as mock_repo, \
             patch("app.presentation.routes.bookings._image_repo") as mock_img, \
             patch("app.presentation.routes.bookings._hw_config_repo") as mock_hw, \
             patch("app.presentation.routes.bookings._namespace_repo") as mock_ns, \
             patch("app.presentation.routes.bookings._static_vm_repo") as mock_svm, \
             patch("app.presentation.routes.bookings._role_repo") as mock_role:
            mock_repo.list_by_user = AsyncMock(return_value=[])
            mock_img.list_active = AsyncMock(return_value=[])
            mock_hw.list_active = AsyncMock(return_value=[])
            mock_ns.list_available = AsyncMock(return_value=[])
            mock_svm.list_available = AsyncMock(return_value=[])
            mock_role.list_active = AsyncMock(return_value=[_A_ROLE])

            resp = client.get("/")

            assert resp.status_code == 200
            mock_role.list_active.assert_called_once()
    finally:
        from app.main import app
        app.dependency_overrides.clear()

    assert 'id="roles-dropdown"' in resp.text
    assert "docker-machine" in resp.text


# ── POST /bookings — server-side defense in depth ─────────────────────────────

def test_non_admin_post_bookings_roles_silently_ignored():
    """A non-admin POSTing `roles` directly (bypassing the hidden UI) still creates the
    booking, with the roles ignored rather than applied or erroring."""
    from tests.conftest import make_fake_user

    client = _client_as(make_fake_user())
    try:
        booking = _vm_booking()
        with patch("app.presentation.routes.bookings._use_case") as mock_uc, \
             patch("app.presentation.routes.bookings._role_repo") as mock_role_repo:
            mock_uc.execute = AsyncMock(return_value=booking)
            resp = client.post("/bookings", data={
                "resource_type": "VM", "ttl_minutes": "240",
                "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
                "roles": ["docker-machine"],
            })

            assert resp.status_code == 201
            kwargs = mock_uc.execute.call_args.kwargs
            assert kwargs["config_roles"] == []
            # Never even attempted to resolve the (ignored) role name.
            mock_role_repo.get_by_name.assert_not_called()
    finally:
        from app.main import app
        app.dependency_overrides.clear()


def test_admin_post_bookings_roles_still_applied():
    """Regression: admins posting `roles` still get them resolved and applied."""
    from tests.conftest import make_fake_admin

    client = _client_as(make_fake_admin())
    try:
        booking = _vm_booking()
        with patch("app.presentation.routes.bookings._use_case") as mock_uc, \
             patch("app.presentation.routes.bookings._role_repo") as mock_role_repo:
            mock_uc.execute = AsyncMock(return_value=booking)
            mock_role_repo.get_by_name = AsyncMock(return_value=_A_ROLE)
            resp = client.post("/bookings", data={
                "resource_type": "VM", "ttl_minutes": "240",
                "image_id": str(uuid4()), "hw_config_id": str(uuid4()),
                "roles": ["docker-machine"],
            })

            assert resp.status_code == 201
            kwargs = mock_uc.execute.call_args.kwargs
            assert kwargs["config_roles"] == [{
                "name": "docker-machine", "ansible_role": "docker_machine",
                "vars": {}, "secret_vars": {},
            }]
    finally:
        from app.main import app
        app.dependency_overrides.clear()
