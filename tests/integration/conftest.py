"""Fixtures for Postgres integration tests.

Run with:
    TEST_POSTGRES_URL=postgresql+asyncpg://portal:portal@host:5433/portal_test pytest -m integration

The default URL assumes a local test container on port 5433 (see docs/development.md).
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator
from uuid import uuid4

import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    HWConfigModel,
    NamespaceModel,
    QuotaModel,
    VMImageModel,
)

_DEFAULT_URL = "postgresql+asyncpg://portal:portal@localhost:5433/portal_test"
_ASYNC_URL = os.environ.get("TEST_POSTGRES_URL", _DEFAULT_URL)
_SYNC_URL = _ASYNC_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
_REPO_ROOT = Path(__file__).parent.parent.parent


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Session-scoped engine; runs Alembic migrations once before any test."""
    import pytest
    engine = create_async_engine(_ASYNC_URL, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not available at {_ASYNC_URL}: {exc}")

    old_url = os.environ.get("DATABASE_URL_SYNC")
    os.environ["DATABASE_URL_SYNC"] = _SYNC_URL
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")
    finally:
        if old_url is None:
            os.environ.pop("DATABASE_URL_SYNC", None)
        else:
            os.environ["DATABASE_URL_SYNC"] = old_url

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def seed_catalog(async_engine: AsyncEngine) -> dict:
    """Insert catalog rows (VMImage, HWConfig, Namespace) once per session.

    Yields a dict with ``image_id``, ``hw_id``, and ``ns_id`` UUIDs for use in tests.
    Cleans up catalog rows after the session.
    """
    image_id = uuid4()
    hw_id = uuid4()
    ns_id = uuid4()
    now = datetime.now(timezone.utc)

    async with AsyncSession(async_engine, expire_on_commit=False) as session:
        session.add(VMImageModel(
            id=image_id, name=f"inttest-image-{image_id}",
            vapp_template_id="tpl-inttest", is_active=True, created_at=now,
        ))
        session.add(HWConfigModel(
            id=hw_id, name=f"inttest-hw-{hw_id}",
            cpus=3, memory_mb=3072, disk_mb=26624, drive_type="HDD",
            is_active=True, created_at=now,
        ))
        session.add(NamespaceModel(
            id=ns_id, name="inttest-ns-01", cluster_name=f"inttest-cluster-{ns_id}",
            api_url=None, is_active=True, created_at=now,
        ))
        await session.commit()

    yield {"image_id": image_id, "hw_id": hw_id, "ns_id": ns_id}

    async with AsyncSession(async_engine) as session:
        await session.execute(delete(VMImageModel).where(VMImageModel.id == image_id))
        await session.execute(delete(HWConfigModel).where(HWConfigModel.id == hw_id))
        await session.execute(delete(NamespaceModel).where(NamespaceModel.id == ns_id))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def async_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Function-scoped session wrapped in a SAVEPOINT that rolls back after each test.

    Repos that call ``session.commit()`` release the savepoint and create a new one;
    the outer connection-level ``ROLLBACK`` undoes all test writes so no data leaks
    between tests. Use ``async_engine`` directly for tests that need concurrent
    sessions or must verify real commit semantics.
    """
    async with async_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        yield session
        await session.close()
        await conn.rollback()
