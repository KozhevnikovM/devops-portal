"""Shared test helpers — available to all test modules."""
from datetime import datetime, timezone
from uuid import uuid4

from app.domain.entities import User


def make_fake_admin() -> User:
    """Return a fake admin user for use in dependency overrides."""
    return User(
        id=uuid4(),
        username="test-admin",
        password_hash="",
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
