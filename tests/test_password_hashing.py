"""Regression tests for #493 — password hashing/verification runs off the event loop, on a
dedicated thread pool bounded by BCRYPT_MAX_CONCURRENCY, and `app.infrastructure.passwords`
is the only module that touches bcrypt."""
import asyncio
import os
import re
import threading
import time
from pathlib import Path

import bcrypt
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.infrastructure import passwords
from app.infrastructure.passwords import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    hash_password_blocking,
    verify_password,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(autouse=True)
def _fresh_executor():
    passwords._reset_executor_for_tests()
    yield
    passwords._reset_executor_for_tests()


# ── Setting ───────────────────────────────────────────────────────────────────

def test_max_concurrency_defaults_to_unset():
    assert Settings().BCRYPT_MAX_CONCURRENCY is None


@pytest.mark.parametrize("bad", [0, -1])
def test_max_concurrency_rejects_non_positive(bad):
    with pytest.raises(ValidationError):
        Settings(BCRYPT_MAX_CONCURRENCY=bad)


# ── Behaviour is unchanged ────────────────────────────────────────────────────

async def test_hash_then_verify_round_trips():
    hashed = await hash_password("correct horse battery staple")
    assert await verify_password("correct horse battery staple", hashed) is True


async def test_verify_rejects_wrong_password():
    hashed = await hash_password("correct horse battery staple")
    assert await verify_password("wrong password", hashed) is False


async def test_hash_is_a_standard_bcrypt_hash():
    hashed = await hash_password("some-password")
    assert bcrypt.checkpw(b"some-password", hashed.encode()) is True


async def test_pre_existing_bcrypt_hash_still_verifies():
    legacy = bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode()
    assert await verify_password("old-password", legacy) is True


def test_dummy_hash_has_the_same_cost_as_real_hashes():
    real = hash_password_blocking("x")
    assert DUMMY_PASSWORD_HASH[:7] == real[:7]  # "$2b$12$" — algorithm + cost factor


# ── Off the event loop ────────────────────────────────────────────────────────

async def _assert_loop_progresses_during(make_work):
    """A canary ticking every 5 ms must keep ticking while the password work is in flight.
    Inline bcrypt would freeze the loop for the whole call, so the canary couldn't tick."""
    ticks_during = 0
    in_flight = False
    stop = asyncio.Event()

    async def canary():
        nonlocal ticks_during
        while not stop.is_set():
            if in_flight:
                ticks_during += 1
            await asyncio.sleep(0.005)

    canary_task = asyncio.create_task(canary())
    await asyncio.sleep(0.02)
    in_flight = True
    start = time.monotonic()
    await make_work()
    elapsed = time.monotonic() - start
    in_flight = False
    stop.set()
    await canary_task

    assert elapsed > 0.05, "bcrypt finished suspiciously fast — test isn't exercising real cost"
    assert ticks_during >= 3, f"event loop stalled during password work ({ticks_during} canary ticks)"


async def test_hash_password_does_not_block_the_event_loop():
    await _assert_loop_progresses_during(lambda: hash_password("some-password"))


async def test_verify_password_does_not_block_the_event_loop():
    hashed = hash_password_blocking("some-password")
    await _assert_loop_progresses_during(lambda: verify_password("some-password", hashed))


# ── Bounded concurrency ───────────────────────────────────────────────────────

async def test_concurrency_is_capped_by_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "BCRYPT_MAX_CONCURRENCY", 2)
    lock = threading.Lock()
    running = peak = 0
    real_checkpw = bcrypt.checkpw

    def counting_checkpw(pw, hashed):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.05)  # hold the slot long enough for the others to pile up
            return real_checkpw(pw, hashed)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(passwords.bcrypt, "checkpw", counting_checkpw)
    hashed = hash_password_blocking("pw-12345")

    results = await asyncio.gather(*(
        verify_password("pw-12345" if i % 2 == 0 else "wrong", hashed) for i in range(6)
    ))

    assert peak <= 2
    assert peak == 2, "the limit was never reached — the test didn't exercise queuing"
    assert results == [i % 2 == 0 for i in range(6)]


async def test_default_limit_is_the_available_cpu_count(monkeypatch):
    monkeypatch.setattr(settings, "BCRYPT_MAX_CONCURRENCY", None)
    expected = (
        os.process_cpu_count() if hasattr(os, "process_cpu_count") else len(os.sched_getaffinity(0))
    )
    await hash_password("x")  # creates the pool lazily
    assert passwords._executor is not None
    assert passwords._executor._max_workers == expected


async def test_password_work_does_not_use_the_default_executor():
    """Password work runs on its own pool, not on the loop's shared default executor."""
    thread_names = []
    real = passwords.hash_password_blocking

    def recording(pw):
        thread_names.append(threading.current_thread().name)
        return real(pw)

    passwords.hash_password_blocking = recording
    try:
        await hash_password("x")
    finally:
        passwords.hash_password_blocking = real
    assert thread_names and thread_names[0].startswith("bcrypt")


# ── Structural: one seam owns bcrypt ──────────────────────────────────────────

def test_only_the_passwords_module_imports_bcrypt():
    offenders = [
        str(path.relative_to(APP_DIR.parent))
        for path in APP_DIR.rglob("*.py")
        if path.name != "passwords.py"
        and re.search(r"^\s*(import bcrypt|from bcrypt\b)", path.read_text(), re.MULTILINE)
    ]
    assert offenders == []


def test_auth_routes_call_password_helpers_only_through_the_session_wrappers():
    """routes/auth.py must reach hash_password/verify_password only via `_hash`/`_verify`, which
    commit the request session first — so no route can await password work while holding a DB
    connection (design D3)."""
    import ast

    source = (APP_DIR / "presentation" / "routes" / "auth.py").read_text()
    tree = ast.parse(source)
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"hash_password", "verify_password"}
                and func.name not in {"_hash", "_verify"}
            ):
                offenders.append(f"{func.name}:{node.lineno}")
    assert offenders == []
    assert "_hash" in source and "_verify" in source


# ── Route level: an in-flight login doesn't hold up other requests ────────────

async def test_unrelated_request_completes_while_a_login_is_verifying():
    """Hold a login inside password verification, then send an unrelated request to the same
    app on the same loop: it must complete while the login is still waiting."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from app.main import app

    entered, release = asyncio.Event(), asyncio.Event()

    async def held_verify(password, password_hash):
        entered.set()
        await release.wait()
        return False

    repo = MagicMock()
    repo.get_by_username = AsyncMock(return_value=None)
    transport = httpx.ASGITransport(app=app)
    with (
        patch("app.presentation.routes.auth._user_repo", repo),
        patch("app.presentation.routes.auth.verify_password", held_verify),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = asyncio.create_task(
                client.post("/auth/login", data={"username": "ghost", "password": "whatever"})
            )
            await asyncio.wait_for(entered.wait(), timeout=5)

            other = await asyncio.wait_for(client.get("/auth/login"), timeout=5)
            assert other.status_code == 200
            assert not login.done(), "login finished early — the test didn't overlap the two requests"

            release.set()
            resp = await asyncio.wait_for(login, timeout=5)
    assert resp.status_code == 401
