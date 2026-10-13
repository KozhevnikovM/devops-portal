"""Blank optional UUID form fields mean "no explicit selection" (#525).

A browser submits the order forms' default option ("Blueprint default" / "Any available") as
`field=""`, not by omitting the field — so these tests post exactly that, and also post the
default option's value as the server renders it.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.entities import Booking, Environment, EnvironmentBlueprint
from app.domain.enums import BookingStatus, ResourceType
from app.domain.pagination import EnvironmentPage, KeysetPage

_BOOKINGS = "app.presentation.routes.bookings"
_ENVIRONMENTS = "app.presentation.routes.environments"


@pytest.fixture
def client():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app
    from tests.conftest import make_fake_admin

    fake_user = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: fake_user
    yield TestClient(app), fake_user
    app.dependency_overrides.clear()


def _booking(user, resource_type):
    now = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        user_id=str(user.id),
        status=BookingStatus.READY,
        resource_type=resource_type,
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
    )


def _environment(user):
    now = datetime.now(UTC)
    return Environment(
        id=uuid4(),
        name="dev-stack",
        blueprint_name="dev-stack",
        user_id=str(user.id),
        ttl_minutes=240,
        expires_at=now + timedelta(minutes=240),
        created_at=now,
        bookings=[],
    )


# Each browser-submitted optional UUID field: (route, the use case it reaches, base form data).
_FIELDS = {
    "environment namespace_id": (
        "/environments",
        f"{_ENVIRONMENTS}._order_use_case",
        "namespace_id",
        {"blueprint_name": "dev-stack", "ttl_minutes": "240"},
        None,
    ),
    "booking namespace_id": (
        "/bookings",
        f"{_BOOKINGS}._book_namespace_use_case",
        "namespace_id",
        {"resource_type": "NAMESPACE", "ttl_minutes": "240"},
        ResourceType.NAMESPACE,
    ),
    "booking static_vm_id": (
        "/bookings",
        f"{_BOOKINGS}._reserve_static_vm_use_case",
        "static_vm_id",
        {"resource_type": "STATIC_VM", "ttl_minutes": "240"},
        ResourceType.STATIC_VM,
    ),
}


def _post(client, field_key, value):
    """POST the field with `value`; return (response, the patched use case's execute mock)."""
    cl, user = client
    url, use_case, field, data, resource_type = _FIELDS[field_key]
    result = (
        _environment(user) if resource_type is None else _booking(user, resource_type)
    )
    with patch(use_case) as uc, patch(f"{_BOOKINGS}._repo"):
        uc.execute = AsyncMock(return_value=result)
        resp = cl.post(url, data={**data, field: value})
    return resp, uc.execute


@pytest.mark.parametrize("field_key", _FIELDS)
def test_blank_value_means_no_explicit_selection(client, field_key):
    resp, execute = _post(client, field_key, "")
    assert resp.status_code == 201
    field = _FIELDS[field_key][2]
    assert execute.call_args.kwargs[field] is None


@pytest.mark.parametrize("field_key", _FIELDS)
def test_valid_uuid_is_forwarded(client, field_key):
    chosen = uuid4()
    resp, execute = _post(client, field_key, str(chosen))
    assert resp.status_code == 201
    field = _FIELDS[field_key][2]
    assert execute.call_args.kwargs[field] == chosen


@pytest.mark.parametrize("field_key", _FIELDS)
def test_malformed_uuid_is_rejected(client, field_key):
    resp, execute = _post(client, field_key, "junk")
    assert resp.status_code == 422
    execute.assert_not_called()


# ── The default option, as rendered, round-trips through its endpoint ─────────────────────────


def _default_option_value(html, select_name):
    """The `value` of the first <option> inside `<select name="select_name">`."""
    select = re.search(
        rf'<select[^>]*name="{select_name}"[^>]*>(.*?)</select>', html, re.DOTALL
    )
    assert select, f"no <select name={select_name!r}> rendered"
    option = re.search(r'<option[^>]*value="([^"]*)"', select.group(1))
    assert option, f"<select name={select_name!r}> has no options"
    return option.group(1)


def _render_booking_page(cl, path):
    ns = SimpleNamespace(id=uuid4(), name="team-a-dev", cluster_name="prod-cluster")
    svm = SimpleNamespace(id=uuid4(), name="build-agent-1", host="10.0.0.12")
    with (
        patch(f"{_BOOKINGS}._repo") as repo,
        patch(f"{_BOOKINGS}._image_repo") as img,
        patch(f"{_BOOKINGS}._hw_config_repo") as hw,
        patch(f"{_BOOKINGS}._namespace_repo") as nsr,
        patch(f"{_BOOKINGS}._static_vm_repo") as svmr,
        patch(f"{_BOOKINGS}._role_repo") as role,
    ):
        repo.list_page = AsyncMock(return_value=KeysetPage(items=[]))
        img.list_active = AsyncMock(return_value=[])
        hw.list_active = AsyncMock(return_value=[])
        nsr.list_available = AsyncMock(return_value=[ns])
        svmr.list_available = AsyncMock(return_value=[svm])
        role.list_active = AsyncMock(return_value=[])
        resp = cl.get(path)
    assert resp.status_code == 200
    return resp.text


def _render_environments_page(cl):
    ns = SimpleNamespace(id=uuid4(), name="dev1", cluster_name="prod-cluster")
    blueprint = EnvironmentBlueprint(
        id=uuid4(),
        name="dev-stack",
        description="",
        is_active=True,
        created_at=datetime.now(UTC),
        items=[],
    )
    with (
        patch(f"{_ENVIRONMENTS}._env_repo") as er,
        patch(f"{_ENVIRONMENTS}._blueprint_repo") as br,
        patch(f"{_ENVIRONMENTS}._namespace_repo") as nr,
    ):
        er.list_page = AsyncMock(return_value=EnvironmentPage(items=[]))
        br.list_active = AsyncMock(return_value=[blueprint])
        nr.list_available = AsyncMock(return_value=[ns])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = cl.get("/environments")
    assert resp.status_code == 200
    return resp.text


def test_environment_form_blueprint_default_round_trips(client):
    value = _default_option_value(_render_environments_page(client[0]), "namespace_id")
    resp, execute = _post(client, "environment namespace_id", value)
    assert resp.status_code == 201
    assert execute.call_args.kwargs["namespace_id"] is None


def test_namespace_form_any_available_round_trips(client):
    value = _default_option_value(
        _render_booking_page(client[0], "/book/namespace"), "namespace_id"
    )
    resp, execute = _post(client, "booking namespace_id", value)
    assert resp.status_code == 201
    assert execute.call_args.kwargs["namespace_id"] is None


def test_static_vm_form_any_available_round_trips(client):
    value = _default_option_value(
        _render_booking_page(client[0], "/book/vm"), "static_vm_id"
    )
    resp, execute = _post(client, "booking static_vm_id", value)
    assert resp.status_code == 201
    assert execute.call_args.kwargs["static_vm_id"] is None
