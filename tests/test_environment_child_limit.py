"""The environment child limit, ENVIRONMENT_MAX_CHILDREN (#497).

An environment gets one child per blueprint item, so the limit is enforced where blueprints are
saved (admin page + JSON API) and where they are ordered (covering blueprints saved before the
limit existed). Page reconciliation's bounded per-environment child read relies on it.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.domain.exceptions import EnvironmentItemError, EnvironmentTooLargeError
from app.domain.validation import validate_environment_size
from tests.test_environment_ordering import _blueprint, _bp_item, _make_use_case

LIMIT = settings.ENVIRONMENT_MAX_CHILDREN


def _ns_items(n):
    return [_bp_item("NAMESPACE", {}, f"ns{i}", i) for i in range(n)]


def _json_items(n):
    return [{"resource_type": "NAMESPACE", "label": f"ns{i}", "spec": {}} for i in range(n)]


# ── validator ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("count", [0, 4, 5])
def test_validator_accepts_up_to_the_limit(count):
    validate_environment_size(count, 5)


def test_validator_rejects_above_the_limit_and_names_it():
    with pytest.raises(EnvironmentTooLargeError, match="at most 5 items"):
        validate_environment_size(6, 5)


def test_too_large_is_an_item_error():
    # So every existing EnvironmentItemError mapping (API 400, HTML form error) covers it.
    assert issubclass(EnvironmentTooLargeError, EnvironmentItemError)


# ── order use case ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_order_over_the_limit_creates_and_reserves_nothing():
    bp = _blueprint(_ns_items(4))
    uc, m = _make_use_case(bp)
    uc._max_children = 3
    with pytest.raises(EnvironmentTooLargeError, match="at most 3 items"):
        await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    m.env_repo.create.assert_not_called()
    m.ns_uc.execute.assert_not_called()
    m.static_uc.execute.assert_not_called()
    m.create_uc.execute.assert_not_called()
    m.dispatcher.dispatch_provision.assert_not_called()


@pytest.mark.asyncio
async def test_order_at_the_limit_creates_every_child():
    bp = _blueprint(_ns_items(3))
    uc, m = _make_use_case(bp)
    uc._max_children = 3
    await uc.execute(MagicMock(), "dev-stack", 240, user_id="u")
    m.env_repo.create.assert_awaited_once()
    assert m.ns_uc.execute.await_count == 3


def test_app_use_case_is_wired_with_the_configured_limit():
    from app.presentation.deps import order_environment_uc
    assert order_environment_uc._max_children == LIMIT


# ── order routes ──────────────────────────────────────────────────────────────
@pytest.fixture
def user_client():
    from app.main import app
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_user

    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: make_fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client():
    from app.main import app
    from app.infrastructure.auth import require_admin, require_user
    from app.infrastructure.database.session import get_async_session
    from tests.conftest import make_fake_admin

    admin = make_fake_admin()
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/environments", "/api/v1/environments"])
def test_api_order_over_the_limit_400(user_client, path):
    with patch("app.presentation.routes.api_environments._order_use_case") as uc:
        uc.execute = AsyncMock(side_effect=EnvironmentTooLargeError(f"a blueprint may have at most {LIMIT} items"))
        resp = user_client.post(path, json={"blueprint_name": "huge", "ttl_minutes": 240})
    assert resp.status_code == 400
    assert f"at most {LIMIT} items" in resp.json()["detail"]


def test_html_order_over_the_limit_shows_form_error(user_client):
    with patch("app.presentation.routes.environments._order_use_case") as uc, \
         patch("app.presentation.routes.environments._blueprint_repo") as br, \
         patch("app.presentation.routes.environments._namespace_repo") as nr:
        uc.execute = AsyncMock(side_effect=EnvironmentTooLargeError(f"a blueprint may have at most {LIMIT} items"))
        br.list_active = AsyncMock(return_value=[])
        nr.list_available = AsyncMock(return_value=[])
        nr.list_held_standalone_by_user = AsyncMock(return_value=[])
        resp = user_client.post("/environments", data={"blueprint_name": "huge", "ttl_minutes": "240"})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#environment-order-form"
    assert f"at most {LIMIT} items" in resp.text


# ── blueprint saves ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("prefix", ["/api", "/api/v1"])
def test_api_create_blueprint_over_the_limit_422(admin_client, prefix):
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.create = AsyncMock()
        resp = admin_client.post(f"{prefix}/environment-blueprints",
                                 json={"name": "huge", "items": _json_items(LIMIT + 1)})
    assert resp.status_code == 422
    assert f"at most {LIMIT} items" in resp.json()["detail"]
    repo.create.assert_not_called()


def test_api_create_blueprint_at_the_limit_is_saved(admin_client):
    from tests.test_environment_blueprint_catalog import _bp
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.create = AsyncMock(return_value=_bp(name="full"))
        resp = admin_client.post("/api/environment-blueprints",
                                 json={"name": "full", "items": _json_items(LIMIT)})
    assert resp.status_code == 201
    assert len(repo.create.call_args.args[3]) == LIMIT


def test_api_update_blueprint_over_the_limit_leaves_it_unchanged(admin_client):
    from uuid import uuid4
    with patch("app.presentation.routes.api._blueprint_repo") as repo:
        repo.update = AsyncMock()
        resp = admin_client.patch(f"/api/environment-blueprints/{uuid4()}",
                                  json={"items": _json_items(LIMIT + 1)})
    assert resp.status_code == 422
    repo.update.assert_not_called()


def test_admin_create_blueprint_over_the_limit_shows_form_error(admin_client):
    with patch("app.presentation.routes.admin._blueprint_repo") as repo:
        repo.create = AsyncMock()
        resp = admin_client.post("/admin/catalog/blueprints", data={
            "name": "huge", "items": json.dumps(_json_items(LIMIT + 1)),
        })
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#blueprint-create-error"
    assert f"at most {LIMIT} items" in resp.text
    repo.create.assert_not_called()


def test_admin_update_blueprint_over_the_limit_leaves_it_unchanged(admin_client):
    from uuid import uuid4
    with patch("app.presentation.routes.admin._blueprint_repo") as repo:
        repo.update = AsyncMock()
        resp = admin_client.patch(f"/admin/catalog/blueprints/{uuid4()}", data={
            "name": "huge", "items": json.dumps(_json_items(LIMIT + 1)),
        })
    assert resp.status_code == 200
    assert f"at most {LIMIT} items" in resp.text
    repo.update.assert_not_called()


# ── startup: effective limit ──────────────────────────────────────────────────
def test_effective_limit_raised_by_legacy_live_environment(caplog):
    from app import main
    with patch("app.main.SyncSessionLocal"), \
         patch.object(main.EnvironmentRepository, "sync_live_children_over", return_value=(LIMIT + 7, 2)):
        with caplog.at_level("WARNING", logger="app.main"):
            assert main._effective_environment_child_limit() == LIMIT + 7
    assert f"2 live environment(s) exceed ENVIRONMENT_MAX_CHILDREN={LIMIT}" in caplog.text
    assert f"largest: {LIMIT + 7} children" in caplog.text


def test_effective_limit_is_the_configured_one_otherwise(caplog):
    from app import main
    with patch("app.main.SyncSessionLocal"), \
         patch.object(main.EnvironmentRepository, "sync_live_children_over", return_value=(3, 0)):
        with caplog.at_level("WARNING", logger="app.main"):
            assert main._effective_environment_child_limit() == LIMIT
    assert "ENVIRONMENT_MAX_CHILDREN" not in caplog.text
