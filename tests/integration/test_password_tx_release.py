"""Integration tests for #493 (design D3): no request holds a DB transaction or pooled connection
while it awaits password hashing/verification — on every password path, including the
transaction the real `require_user`/`require_admin` dependency opens on the shared session.

Real PostgreSQL session and real auth dependencies; only Redis is faked (the CI integration job
has no Redis). Spies wrap the *raw* helpers (`hash_password`/`verify_password`), so a route that
bypassed the committing `_hash`/`_verify` wrappers would be caught here too.
"""
import json
import secrets
from collections.abc import AsyncGenerator
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.infrastructure.database.models import APIKeyModel, UserModel
from app.infrastructure.database.session import get_async_session
from app.infrastructure.passwords import hash_password_blocking
from app.infrastructure.repositories.user_repo import UserRepository
from app.main import app
from app.presentation.routes import auth as routes_auth

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]

PASSWORD = "original-pw-493"
NEW_PASSWORD = "brand-new-pw-493"


class FakeRedis:
    """Just enough of redis.asyncio for cookie sessions and session invalidation."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def setex(self, key, _ttl, value):
        self.kv[key] = value

    async def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def expire(self, _key, _ttl):
        return True

    async def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)
            self.sets.pop(key, None)

    async def aclose(self):
        pass


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncGenerator[dict, None]:
    """An admin (with an API key), a regular user and a reset target, all committed for real;
    requests run on real sessions of the test engine; every password await is observed."""
    tag = uuid4().hex[:8]
    prefix = f"pwtx-{tag}-"
    ids = {name: uuid4() for name in ("admin", "user", "target")}
    async with AsyncSession(async_engine, expire_on_commit=False) as s:
        for name, uid in ids.items():
            s.add(UserModel(
                id=uid, username=f"{prefix}{name}",
                password_hash=hash_password_blocking(PASSWORD),
                role="admin" if name == "admin" else "user",
            ))
        await s.commit()
        raw_key, _ = await UserRepository().create_api_key(s, ids["admin"], "pwtx")

    fake = FakeRedis()

    def cookie_for(name: str) -> dict:
        sid = secrets.token_hex(16)
        fake.kv[f"session:{sid}"] = json.dumps({"user_id": str(ids[name]), "username": name, "role": "x"})
        fake.sets.setdefault(f"user_sessions:{ids[name]}", set()).add(sid)
        return {"session_id": sid}

    request_sessions: list[AsyncSession] = []

    async def session_override():
        async with AsyncSession(async_engine, expire_on_commit=False) as s:
            request_sessions.append(s)
            yield s

    observed: list[tuple[str, bool, int]] = []
    pool = async_engine.sync_engine.pool
    real_hash, real_verify = routes_auth.hash_password, routes_auth.verify_password

    async def spy_hash(password):
        observed.append(("hash", request_sessions[-1].in_transaction(), pool.checkedout()))
        return await real_hash(password)

    async def spy_verify(password, password_hash):
        observed.append(("verify", request_sessions[-1].in_transaction(), pool.checkedout()))
        return await real_verify(password, password_hash)

    app.dependency_overrides[get_async_session] = session_override
    with (
        patch("app.infrastructure.auth._get_redis", return_value=fake),
        patch("app.presentation.routes.auth._get_redis", return_value=fake),
        patch("app.presentation.routes.auth.hash_password", spy_hash),
        patch("app.presentation.routes.auth.verify_password", spy_verify),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client, "ids": ids, "prefix": prefix, "api_key": raw_key,
                "cookie_for": cookie_for, "observed": observed, "engine": async_engine,
            }
    app.dependency_overrides.pop(get_async_session, None)

    async with AsyncSession(async_engine) as s:
        user_ids = (await s.execute(
            select(UserModel.id).where(UserModel.username.like(f"{prefix}%"))
        )).scalars().all()
        await s.execute(delete(APIKeyModel).where(APIKeyModel.user_id.in_(user_ids)))
        await s.execute(delete(UserModel).where(UserModel.id.in_(user_ids)))
        await s.commit()


def _assert_released(observed, expected_ops):
    assert [op for op, _, _ in observed] == expected_ops
    for op, in_tx, checked_out in observed:
        assert in_tx is False, f"{op}: request session still in a transaction while awaiting password work"
        assert checked_out == 0, f"{op}: {checked_out} pooled connection(s) held while awaiting password work"


async def test_login(world):
    resp = await world["client"].post(
        "/auth/login", data={"username": f"{world['prefix']}user", "password": PASSWORD},
    )
    assert resp.status_code == 302
    _assert_released(world["observed"], ["verify"])


async def test_login_unknown_user(world):
    resp = await world["client"].post(
        "/auth/login", data={"username": f"{world['prefix']}nobody", "password": PASSWORD},
    )
    assert resp.status_code == 401
    _assert_released(world["observed"], ["verify"])


async def test_profile_password_change(world):
    resp = await world["client"].post(
        "/profile/password",
        data={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        cookies=world["cookie_for"]("user"),
    )
    assert resp.status_code == 200
    assert "incorrect" not in resp.text
    _assert_released(world["observed"], ["verify", "hash"])


@pytest.mark.parametrize("auth", ["cookie", "api_key"])
async def test_api_create_user(world, auth):
    # Cookie auth leaves the dependency's SELECT transaction open, so this case fails if the
    # wrapper stops committing. API-key auth commits on its own (get_by_key_hash stamps
    # last_used_at), so that case guards against a future open transaction, not today's one.
    kwargs = (
        {"cookies": world["cookie_for"]("admin")} if auth == "cookie"
        else {"headers": {"Authorization": f"Bearer {world['api_key']}"}}
    )
    resp = await world["client"].post(
        "/api/users",
        json={"username": f"{world['prefix']}new-{auth}", "password": NEW_PASSWORD, "role": "user"},
        **kwargs,
    )
    assert resp.status_code == 201, resp.text
    _assert_released(world["observed"], ["hash"])


async def test_admin_ui_create_user(world):
    resp = await world["client"].post(
        "/admin/users",
        data={"username": f"{world['prefix']}new-ui", "password": NEW_PASSWORD, "role": "user"},
        cookies=world["cookie_for"]("admin"),
    )
    assert resp.status_code == 200
    assert "already taken" not in resp.text
    _assert_released(world["observed"], ["hash"])


async def test_api_admin_reset(world):
    resp = await world["client"].post(
        f"/api/users/{world['ids']['target']}/password",
        json={"new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {world['api_key']}"},
    )
    assert resp.status_code == 204
    _assert_released(world["observed"], ["hash"])


async def test_admin_ui_reset(world):
    resp = await world["client"].post(
        f"/admin/users/{world['ids']['target']}/password",
        data={"new_password": NEW_PASSWORD},
        cookies=world["cookie_for"]("admin"),
    )
    assert resp.status_code == 200
    _assert_released(world["observed"], ["hash"])


async def test_new_password_actually_works_after_change(world):
    """End to end: the hash written after the commit-then-hash path is the one login accepts."""
    client = world["client"]
    resp = await client.post(
        "/profile/password",
        data={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        cookies=world["cookie_for"]("user"),
    )
    assert resp.status_code == 200
    ok = await client.post("/auth/login", data={"username": f"{world['prefix']}user", "password": NEW_PASSWORD})
    old = await client.post("/auth/login", data={"username": f"{world['prefix']}user", "password": PASSWORD})
    assert ok.status_code == 302
    assert old.status_code == 401
