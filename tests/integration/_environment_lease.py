"""Shared helpers for the #434 environment-lease integration tests (real Postgres, sync driver)."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.infrastructure.database.models import (
    BookingAuditModel,
    BookingModel,
    EnvironmentModel,
    NamespaceModel,
)
from tests.integration.conftest import _SYNC_URL


def make_sessionmaker() -> sessionmaker:
    return sessionmaker(create_engine(_SYNC_URL, pool_pre_ping=True), expire_on_commit=False)


def insert_environment(
    Session, child_statuses: list[str], *, ttl_minutes: int = 60,
    expires_at: datetime = PERMANENT_EXPIRES_AT, resource_types: list[str] | None = None,
    construction_complete: bool = True,
) -> tuple[UUID, list[UUID]]:
    """Insert an environment and one child per status; return (env_id, child_ids)."""
    env_id = uuid4()
    child_ids = [uuid4() for _ in child_statuses]
    resource_types = resource_types or ["VM"] * len(child_statuses)
    with Session() as s:
        s.add(EnvironmentModel(
            id=env_id, name=f"inttest-env-{env_id}", blueprint_name=None, user_id="inttest-owner",
            ttl_minutes=ttl_minutes, expires_at=expires_at, construction_complete=construction_complete,
        ))
        s.flush()
        for child_id, status, rt in zip(child_ids, child_statuses, resource_types):
            s.add(BookingModel(
                id=child_id, user_id="inttest-owner", status=status, resource_type=rt,
                ttl_minutes=ttl_minutes, expires_at=PERMANENT_EXPIRES_AT, environment_id=env_id,
                created_at=datetime.now(timezone.utc),
            ))
        s.commit()
    return env_id, child_ids


def expiries(Session, env_id: UUID) -> tuple[datetime, list[datetime]]:
    """The environment's expires_at and every child's, freshly read."""
    with Session() as s:
        env = s.get(EnvironmentModel, env_id)
        children = s.execute(
            select(BookingModel.expires_at).where(BookingModel.environment_id == env_id)
        ).scalars().all()
        return env.expires_at, list(children)


def cleanup(Session, env_ids: list[UUID], namespace_ids: list[UUID] = ()) -> None:
    with Session() as s:
        child_ids = select(BookingModel.id).where(BookingModel.environment_id.in_(env_ids))
        s.execute(delete(BookingAuditModel).where(BookingAuditModel.booking_id.in_(child_ids)))
        s.execute(delete(BookingModel).where(BookingModel.environment_id.in_(env_ids)))
        s.execute(delete(EnvironmentModel).where(EnvironmentModel.id.in_(env_ids)))
        if namespace_ids:
            s.execute(delete(NamespaceModel).where(NamespaceModel.id.in_(list(namespace_ids))))
        s.commit()
