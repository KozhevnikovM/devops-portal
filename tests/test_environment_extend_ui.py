"""Browser Extend action for environments (#532): the row menu form and the HTMX route."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.entities import Booking, Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingPermissionError,
    EnvironmentError,
    EnvironmentNotFoundError,
    InvalidExtensionError,
)

_OWNER = UUID(int=3)
_DISPATCHER = UUID(int=4)
_NOW = datetime.now(UTC)
_EXPIRES = _NOW + timedelta(hours=2)


def _user(uid, role="user", username="someone") -> User:
    return User(
        id=uid,
        username=username,
        password_hash="",
        role=role,
        is_active=True,
        created_at=_NOW,
    )


_OWNER_USER = _user(_OWNER, username="owner")


def _env(*statuses, ttl_minutes=120, expires_at=_EXPIRES) -> Environment:
    children = [
        Booking(
            id=uuid4(),
            user_id=str(_OWNER),
            status=s,
            resource_type=ResourceType.NAMESPACE,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            created_at=_NOW,
        )
        for s in (statuses or [BookingStatus.READY])
    ]
    return Environment(
        id=uuid4(),
        name="stack",
        blueprint_name="bp",
        user_id=str(_OWNER),
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        created_at=_NOW,
        bookings=children,
        owner_username="owner",
        created_by=str(_DISPATCHER),
    )


@pytest.fixture
def as_user():
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()

    def login(user):
        app.dependency_overrides[require_user] = lambda: user
        return TestClient(app)

    yield login
    app.dependency_overrides.clear()


def _row(client, env):
    with patch("app.presentation.routes.environments._env_repo") as er:
        er.get = AsyncMock(return_value=env)
        return client.get(f"/environments/{env.id}/row")


# ── Row menu ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "user",
    [
        _OWNER_USER,
        _user(_DISPATCHER, role="dispatcher"),
        _user(uuid4(), role="admin"),
    ],
    ids=["owner", "creating-dispatcher", "admin"],
)
def test_extend_offered_to_managers_of_eligible_stack(as_user, user):
    env = _env()
    resp = _row(as_user(user), env)
    assert resp.status_code == 200
    assert f'hx-put="/environments/{env.id}/extend"' in resp.text
    assert f'id="environment-{env.id}-extend-error"' in resp.text
    assert ">+24 h<" in resp.text
    assert "Forever" not in resp.text
    # Existing actions stay.
    assert f'hx-patch="/environments/{env.id}/name"' in resp.text
    assert f'hx-delete="/environments/{env.id}"' in resp.text


@pytest.mark.parametrize(
    "env",
    [
        _env(ttl_minutes=0, expires_at=PERMANENT_EXPIRES_AT),
        _env(BookingStatus.READY, BookingStatus.PROVISIONING),
        _env(BookingStatus.READY, BookingStatus.FAILED),
        _env(BookingStatus.RELEASED),
        _env(expires_at=PERMANENT_EXPIRES_AT),
    ],
    ids=["permanent", "provisioning", "failed", "released", "lease-not-started"],
)
def test_extend_hidden_for_ineligible_stack(as_user, env):
    resp = _row(as_user(_OWNER_USER), env)
    assert resp.status_code == 200
    assert "/extend" not in resp.text


def test_extend_hidden_from_users_who_cannot_manage():
    """A non-manager never gets the row route (403), and the template itself hides the action."""
    from app.presentation.routes.environments import _annotate
    from app.presentation.templating import templates

    env = _annotate(_env())
    html = templates.get_template("partials/environment_row.html").render(
        environment=env,
        current_user=_user(uuid4(), role="dispatcher"),
    )
    assert "/extend" not in html


# ── HTMX route ──────────────────────────────────────────────────────────────────
def _put(client, env_id, data, *, result=None, error=None):
    with patch("app.presentation.routes.environments._extend_use_case") as uc:
        uc.execute = AsyncMock(return_value=result, side_effect=error)
        resp = client.put(f"/environments/{env_id}/extend", data=data)
        return resp, uc.execute


def test_success_swaps_row_with_new_expiry(as_user):
    env = _env()
    extended = _env(ttl_minutes=1560, expires_at=_EXPIRES + timedelta(minutes=1440))
    extended.id = env.id
    resp, execute = _put(
        as_user(_OWNER_USER), env.id, {"extend_minutes": "1440"}, result=extended
    )

    assert resp.status_code == 200
    assert "HX-Retarget" not in resp.headers
    assert f'id="environment-{env.id}"' in resp.text
    assert execute.await_args.args[2] == 1440


@pytest.mark.parametrize(
    "error, text",
    [
        (
            BookingPermissionError("not allowed to extend this environment"),
            "not allowed",
        ),
        (EnvironmentError("can only extend READY environments"), "READY"),
        (
            InvalidExtensionError(
                "extend_minutes must be a positive number of minutes"
            ),
            "positive",
        ),
        (EnvironmentNotFoundError("nope"), "not found"),
    ],
    ids=["403", "409-released", "422", "404"],
)
def test_rejection_is_shown_in_row_without_swapping_it(as_user, error, text):
    env_id = uuid4()
    resp, _ = _put(as_user(_OWNER_USER), env_id, {"extend_minutes": "60"}, error=error)

    assert resp.status_code == 200
    assert resp.headers["HX-Retarget"] == f"#environment-{env_id}-extend-error"
    assert resp.headers["HX-Reswap"] == "innerHTML"
    assert text in resp.text
    assert "<tr" not in resp.text  # the row, and its displayed expiry, are left alone


@pytest.mark.parametrize("value", ["", "soon", "1.5"])
def test_non_integer_value_is_shown_in_row(as_user, value):
    env_id = uuid4()
    resp, execute = _put(as_user(_OWNER_USER), env_id, {"extend_minutes": value})
    assert resp.headers["HX-Retarget"] == f"#environment-{env_id}-extend-error"
    execute.assert_not_awaited()


def test_error_message_is_escaped(as_user):
    resp, _ = _put(
        as_user(_OWNER_USER),
        uuid4(),
        {"extend_minutes": "60"},
        error=EnvironmentError("<script>x</script>"),
    )
    assert "<script>" not in resp.text
