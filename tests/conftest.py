"""Shared test helpers — available to all test modules."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import User


def make_fake_user() -> User:
    """Return a fake regular user for use in dependency overrides."""
    return User(
        id=uuid4(),
        username="test-user",
        password_hash="",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


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


@pytest.fixture(autouse=True)
def mock_row_changed_publish():
    """#388: BookingRepository publishes a Redis row-changed notification after every committed
    write. Autouse so the rest of the suite stays independent of a real Redis broker being
    reachable — tests exercising the publish behavior itself (tests/test_sse_row_changed.py) take
    this fixture as a parameter and assert on the yielded mocks directly."""
    with patch("app.infrastructure.repositories.booking_repo.publish_row_changed") as sync_mock, \
         patch("app.infrastructure.repositories.booking_repo.apublish_row_changed",
               new=AsyncMock()) as async_mock:
        yield sync_mock, async_mock
