"""Regression tests for the blocking-bcrypt-exhausts-db-pool fix
(docs/bugfix/blocking-bcrypt-exhausts-db-pool.md): `hash_password`/`verify_password` must behave
identically to calling bcrypt directly, and — the actual regression this exists to catch — must
not block the event loop while hashing/verifying."""
import asyncio
import time

import bcrypt
import pytest

from app.infrastructure.auth import hash_password, verify_password


@pytest.mark.asyncio
async def test_hash_then_verify_round_trips():
    hashed = await hash_password("correct horse battery staple")
    assert await verify_password("correct horse battery staple", hashed) is True


@pytest.mark.asyncio
async def test_verify_rejects_wrong_password():
    hashed = await hash_password("correct horse battery staple")
    assert await verify_password("wrong password", hashed) is False


@pytest.mark.asyncio
async def test_hash_password_produces_a_bcrypt_hash_bcrypt_itself_accepts():
    hashed = await hash_password("some-password")
    assert bcrypt.checkpw(b"some-password", hashed.encode()) is True


@pytest.mark.asyncio
async def test_verify_password_does_not_block_the_event_loop():
    """The whole point of the fix: a bcrypt call running via asyncio.to_thread must not stall
    other coroutines scheduled on the same loop. A canary that increments a counter every time
    it gets a turn should keep ticking *while* verify_password is in flight — if bcrypt were
    still running inline on the loop, the canary would be frozen for the entire hash duration."""
    hashed = await hash_password("some-password")

    ticks = 0
    stop = False

    async def canary():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    canary_task = asyncio.create_task(canary())
    await asyncio.sleep(0.02)  # let the canary get a few ticks in before the bcrypt call starts
    ticks_before = ticks

    start = time.monotonic()
    await verify_password("some-password", hashed)
    elapsed = time.monotonic() - start

    stop = True
    await canary_task

    assert elapsed > 0.02, "bcrypt call finished suspiciously fast — test isn't exercising real cost"
    assert ticks > ticks_before, "canary made no progress while verify_password was running"
