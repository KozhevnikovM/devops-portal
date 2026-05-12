from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

async_engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

# Sync engine used by Celery workers and Alembic
sync_engine = create_engine(settings.DATABASE_URL_SYNC, echo=False)
SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False)


async def get_async_session():
    async with AsyncSessionLocal() as session:
        yield session
