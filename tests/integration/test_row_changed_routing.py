"""Integration (#442, PR #463 review): the lifecycle publish for an environment child reads the
environment's routing without touching the caller's session, under a real Postgres session and a
real mapped ``BookingModel``.

A lookup on the caller's session would (on success) leave it in an open transaction holding a pool
connection while the publish talks to Redis, and (on failure) need a rollback that expires every
instance, so a later read of the booking would be another DB round trip. These tests pin both
down with real commits, which is why they use ``async_engine`` directly instead of the
savepoint-wrapped ``async_session`` fixture.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentModel,
)
from app.infrastructure.events import Routing
from app.infrastructure.repositories import booking_repo as mod
from app.infrastructure.repositories.booking_repo import BookingRepository
from tests.integration.conftest import _SYNC_URL

pytestmark = [pytest.mark.integration, pytest.mark.postgres_integration, pytest.mark.asyncio(loop_scope="session")]


async def _seed(engine: AsyncEngine, catalog: dict) -> tuple:
    """An environment ordered by a dispatcher for an owner, and a child booking with no creator
    (like an adopted namespace booking): the two routings differ."""
    owner, dispatcher = f"it-owner-{uuid4()}", f"it-disp-{uuid4()}"
    env_id = uuid4()
    now = datetime.now(timezone.utc)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(EnvironmentModel(
            id=env_id, name="it-env", blueprint_name=None, user_id=owner,
            ttl_minutes=60, expires_at=now + timedelta(hours=1), created_by=dispatcher,
        ))
        await session.commit()
        booking = await BookingRepository().create(session, Booking(
            id=uuid4(), user_id=owner, status=BookingStatus.PENDING, ttl_minutes=60,
            expires_at=now + timedelta(hours=1), created_at=now,
            image_id=catalog["image_id"], image_name="inttest-image",
            hw_config_id=catalog["hw_id"], hw_config_name="inttest-hw",
            cpus=3, memory_mb=3072, disk_mb=26624, drive_type="HDD",
            environment_id=env_id, created_by=None,
        ))
    return booking.id, env_id, owner, dispatcher


def _failing_routing_query():
    """Make the environment routing read hit a real database error (division by zero) — a
    failure *inside* whichever session runs it, which is what forced the rollback before."""
    return patch.object(mod, "_environment_routing_stmt", lambda environment_id: text("SELECT 1/0"))


async def _cleanup(engine: AsyncEngine, booking_id, env_id) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(delete(BookingAuditModel).where(BookingAuditModel.booking_id == booking_id))
        await session.execute(delete(BookingModel).where(BookingModel.id == booking_id))
        await session.execute(delete(EnvironmentModel).where(EnvironmentModel.id == env_id))
        await session.commit()


async def test_async_lookup_leaves_caller_session_idle(
    async_engine: AsyncEngine, seed_catalog: dict, mock_row_changed_publish,
):
    _, async_publish = mock_row_changed_publish
    booking_id, env_id, owner, dispatcher = await _seed(async_engine, seed_catalog)
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            model = await session.get(BookingModel, booking_id)  # held: the identity map is weak
            await BookingRepository().update_status(session, booking_id, BookingStatus.PROVISIONING)

            assert not session.in_transaction()  # no transaction or connection left pinned
            assert not inspect(model).expired_attributes

        async_publish.assert_awaited_once_with(
            booking_id=booking_id, booking_routing=Routing(owner, None),
            environment_id=env_id, environment_routing=Routing(owner, dispatcher),
        )
    finally:
        await _cleanup(async_engine, booking_id, env_id)


async def test_async_failed_lookup_does_not_expire_or_reload_the_booking(
    async_engine: AsyncEngine, seed_catalog: dict, mock_row_changed_publish,
):
    _, async_publish = mock_row_changed_publish
    booking_id, env_id, owner, _ = await _seed(async_engine, seed_catalog)
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            model = await session.get(BookingModel, booking_id)
            with _failing_routing_query():
                await BookingRepository().update_status(session, booking_id, BookingStatus.PROVISIONING)

            assert not session.in_transaction()  # no rollback-then-refresh round trip happened
            assert not inspect(model).expired_attributes

        async_publish.assert_awaited_once_with(
            booking_id=booking_id, booking_routing=Routing(owner, None),
            environment_id=env_id, environment_routing=None,
        )
    finally:
        await _cleanup(async_engine, booking_id, env_id)


@pytest.mark.parametrize("lookup_fails", [False, True], ids=["lookup-succeeds", "lookup-fails"])
async def test_sync_lookup_leaves_caller_session_idle_and_unexpired(
    async_engine: AsyncEngine, seed_catalog: dict, mock_row_changed_publish, lookup_fails,
):
    sync_publish, _ = mock_row_changed_publish
    booking_id, env_id, owner, dispatcher = await _seed(async_engine, seed_catalog)
    engine = create_engine(_SYNC_URL)
    try:
        with Session(engine, expire_on_commit=False) as session:
            model = session.get(BookingModel, booking_id)
            if lookup_fails:
                with _failing_routing_query():
                    BookingRepository().sync_update_status(session, booking_id, BookingStatus.PROVISIONING)
            else:
                BookingRepository().sync_update_status(session, booking_id, BookingStatus.PROVISIONING)

            assert not session.in_transaction()
            assert not inspect(model).expired_attributes

        sync_publish.assert_called_once_with(
            booking_id=booking_id, booking_routing=Routing(owner, None),
            environment_id=env_id,
            environment_routing=None if lookup_fails else Routing(owner, dispatcher),
        )
    finally:
        engine.dispose()
        await _cleanup(async_engine, booking_id, env_id)
