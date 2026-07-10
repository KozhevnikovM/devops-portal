"""Tests for blueprint-level Ansible variables (#288).

Covers: portal dict injection in rendered playbooks, key validation, apply_roles forwarding,
blueprint spec vars flow through order_environment, and direct VM booking API.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from app.infrastructure.config.ansible import _render_playbook, AnsibleConfigRunner


# ── _render_playbook — portal dict ────────────────────────────────────────────

def test_render_playbook_always_injects_portal_dict():
    play = _render_playbook([{"ansible_role": "myrole", "vars": {}}])
    assert "portal:" in play
    assert "ip:" in play
    assert "label:" in play


def test_render_playbook_portal_contains_ip_and_label():
    play = _render_playbook(
        [{"ansible_role": "myrole", "vars": {}}],
        ip="10.0.0.5", label="meta",
    )
    doc = yaml.safe_load(play)
    portal = doc[0]["vars"]["portal"]
    assert portal["ip"] == "10.0.0.5"
    assert portal["label"] == "meta"


def test_render_playbook_extra_vars_appear_in_portal():
    play = _render_playbook(
        [{"ansible_role": "myrole", "vars": {}}],
        extra_vars={"my_custom_var": "hello", "count": 3},
        ip="1.2.3.4", label="assets",
    )
    doc = yaml.safe_load(play)
    portal = doc[0]["vars"]["portal"]
    assert portal["my_custom_var"] == "hello"
    assert portal["count"] == 3


def test_render_playbook_ip_and_label_override_user_supplied():
    """Auto-injected ip/label always win even if the user passes conflicting extra_vars."""
    play = _render_playbook(
        [{"ansible_role": "myrole", "vars": {}}],
        extra_vars={"ip": "bad", "label": "wrong"},
        ip="10.0.0.5", label="real",
    )
    doc = yaml.safe_load(play)
    portal = doc[0]["vars"]["portal"]
    assert portal["ip"] == "10.0.0.5"
    assert portal["label"] == "real"


def test_render_playbook_portal_coexists_with_secrets():
    """vars block + pre_tasks (secrets) must both appear without conflict."""
    play = _render_playbook(
        [{"ansible_role": "myrole", "vars": {}}],
        secrets_path="/tmp/secrets.yml",
        extra_vars={"env": "prod"},
        ip="1.2.3.4", label="web",
    )
    assert "portal:" in play
    assert "pre_tasks:" in play
    assert "no_log: true" in play


# ── AnsibleConfigRunner — forwards extra_vars / label ─────────────────────────

def test_apply_roles_passes_extra_vars_to_playbook():
    runner = AnsibleConfigRunner()
    captured = {}

    def fake_render(roles, secrets_path=None, extra_vars=None, label="", ip=""):
        captured["extra_vars"] = extra_vars
        captured["label"] = label
        captured["ip"] = ip
        return "- hosts: vm\n  roles:\n    - role: myrole\n"

    booking = MagicMock()
    booking.config_roles = [{"ansible_role": "myrole", "vars": {}, "name": "myrole", "secret_vars": {}}]

    proc = MagicMock()
    proc.stdout = iter([])
    proc.returncode = 0

    with patch("app.infrastructure.config.ansible._render_playbook", side_effect=fake_render), \
         patch("app.infrastructure.config.ansible.subprocess.Popen", return_value=proc), \
         patch("app.infrastructure.config.ansible.settings") as s:
        s.VM_SSH_USER = "root"; s.VM_SSH_PORT = 22; s.VM_SSH_PRIVATE_KEY = ""
        s.ANSIBLE_ROLES_PATH = "/roles"; s.ANSIBLE_COLLECTIONS_PATH = "/collections"
        s.ANSIBLE_TIMEOUT = 60; s.ANSIBLE_VERBOSITY = 0; s.SECRETS_ENCRYPTION_KEY = ""
        runner.apply_roles(
            booking, ip="10.0.0.5", password="pw",
            extra_vars={"env": "prod"}, label="meta",
        )

    assert captured["extra_vars"] == {"env": "prod"}
    assert captured["label"] == "meta"
    assert captured["ip"] == "10.0.0.5"


# ── Key validation ─────────────────────────────────────────────────────────────

def test_valid_var_names_are_accepted():
    from app.application.use_cases.order_environment import _validate_extra_vars
    _validate_extra_vars({"my_var": 1, "CamelCase": 2, "_private": 3, "x1": 4})


def test_hyphenated_key_is_rejected():
    from app.application.use_cases.order_environment import _validate_extra_vars
    from app.domain.exceptions import EnvironmentItemError
    with pytest.raises(EnvironmentItemError, match="my-var"):
        _validate_extra_vars({"my-var": "value"})


def test_key_starting_with_digit_is_rejected():
    from app.application.use_cases.order_environment import _validate_extra_vars
    from app.domain.exceptions import EnvironmentItemError
    with pytest.raises(EnvironmentItemError):
        _validate_extra_vars({"1bad": "value"})


# ── API: direct VM booking accepts vars ───────────────────────────────────────

@pytest.fixture
def client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _vm_booking(extra_vars=None):
    from datetime import datetime, timedelta, timezone
    from app.domain.entities import Booking
    from app.domain.enums import BookingStatus, ResourceType
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u", status=BookingStatus.PENDING, resource_type=ResourceType.VM,
        ttl_minutes=240, expires_at=now + timedelta(minutes=240), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
        extra_vars=extra_vars or {},
    )


def test_create_booking_with_vars_passes_extra_vars(client):
    booking = _vm_booking({"env": "staging"})
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw, \
         patch("app.presentation.routes.api_bookings._role_repo") as roles, \
         patch("app.presentation.routes.api_bookings._create_use_case") as uc:
        img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        roles.get_by_name = AsyncMock(return_value=None)
        uc.execute = AsyncMock(return_value=booking)
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "Ubuntu", "hw_config_name": "medium",
            "vars": {"env": "staging"},
        })
    assert resp.status_code == 201
    assert uc.execute.call_args.kwargs["extra_vars"] == {"env": "staging"}


def test_create_booking_rejects_hyphenated_var_name(client):
    with patch("app.presentation.routes.api_bookings._image_repo") as img, \
         patch("app.presentation.routes.api_bookings._hw_config_repo") as hw:
        img.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        hw.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        resp = client.post("/api/bookings", json={
            "resource_type": "VM", "ttl_minutes": 240,
            "image_name": "Ubuntu", "hw_config_name": "medium",
            "vars": {"bad-name": "value"},
        })
    assert resp.status_code == 422
    assert "bad-name" in resp.json()["detail"]


# ── order_environment _resolve_item: spec.vars → extra_vars ──────────────────

@pytest.mark.asyncio
async def test_resolve_item_reads_spec_vars():
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase

    uc = OrderEnvironmentUseCase.__new__(OrderEnvironmentUseCase)
    uc._image_repo = MagicMock()
    uc._image_repo.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    uc._hw_config_repo = MagicMock()
    uc._hw_config_repo.get_by_name = AsyncMock(
        return_value=SimpleNamespace(id=uuid4()))
    uc._role_repo = MagicMock()
    uc._role_repo.get_by_name = AsyncMock(return_value=None)

    item = SimpleNamespace(
        resource_type="VM",
        spec={
            "image_name": "Ubuntu", "hw_config_name": "medium",
            "roles": [],
            "vars": {"deploy_env": "prod", "replicas": 2},
        },
    )
    with patch("app.application.use_cases.order_environment.settings") as s:
        s.SECRET_VARS_ENABLED = True
        result = await uc._resolve_item(None, item)

    assert result["extra_vars"] == {"deploy_env": "prod", "replicas": 2}


@pytest.mark.asyncio
async def test_resolve_item_rejects_invalid_var_name():
    from app.application.use_cases.order_environment import OrderEnvironmentUseCase
    from app.domain.exceptions import EnvironmentItemError

    uc = OrderEnvironmentUseCase.__new__(OrderEnvironmentUseCase)
    uc._image_repo = MagicMock()
    uc._image_repo.get_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    uc._hw_config_repo = MagicMock()
    uc._hw_config_repo.get_by_name = AsyncMock(
        return_value=SimpleNamespace(id=uuid4()))
    uc._role_repo = MagicMock()

    item = SimpleNamespace(
        resource_type="VM",
        spec={
            "image_name": "Ubuntu", "hw_config_name": "medium",
            "vars": {"bad-key": "value"},
        },
    )
    with patch("app.application.use_cases.order_environment.settings") as s:
        s.SECRET_VARS_ENABLED = True
        with pytest.raises(EnvironmentItemError, match="bad-key"):
            await uc._resolve_item(None, item)
