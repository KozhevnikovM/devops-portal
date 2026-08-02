"""Regression tests for #388 — BookingRepository publishes a Redis row-changed notification
after every committed write that changes a booking row's rendered state, and never before/without
a commit. See docs/features/sse-live-updates.md.

The autouse ``mock_row_changed_publish`` fixture (tests/conftest.py) mocks
``publish_row_changed``/``apublish_row_changed`` at the ``booking_repo`` module namespace for the
whole suite; these tests take it as a parameter to assert on the same mocks.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import IllegalStatusTransitionError


def _model(**kw):
    defaults = dict(
        id=uuid4(), status=BookingStatus.PROVISIONING.value, environment_id=None,
        vm_ip=None, vm_password=None, config_failed=False, status_message=None,
        provisioning_log=None, ttl_minutes=240, expires_at=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── sync_update_status ───────────────────────────────────────────────────────────
def test_sync_update_status_publishes_after_commit(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_called_once()
    sync_mock.assert_called_once_with(booking_id=model.id, environment_id=env_id)


def test_sync_update_status_no_publish_on_illegal_transition(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    model = _model(status=BookingStatus.RELEASED.value)  # terminal — no transition is legal
    session = MagicMock(); session.get.return_value = model

    with pytest.raises(IllegalStatusTransitionError):
        BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_not_called()
    sync_mock.assert_not_called()


# ── sync_set_status_message / sync_record_progress ──────────────────────────────
def test_sync_set_status_message_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_set_status_message(session, model.id, "hello")

    sync_mock.assert_called_once_with(booking_id=model.id, environment_id=env_id)


def test_sync_record_progress_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_record_progress(session, model.id, "log line")

    sync_mock.assert_called_once_with(booking_id=model.id, environment_id=env_id)


# ── async update_status / extend / update_label ─────────────────────────────────
@pytest.mark.asyncio
async def test_async_update_status_publishes_after_commit(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()  # sync method on the real AsyncSession
    result = MagicMock(); result.scalar_one_or_none = lambda: model
    session.execute = AsyncMock(return_value=result)

    await BookingRepository().update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_awaited_once()
    async_mock.assert_awaited_once_with(booking_id=model.id, environment_id=env_id)


@pytest.mark.asyncio
async def test_async_update_status_no_publish_on_illegal_transition(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    model = _model(status=BookingStatus.RELEASED.value)
    session = AsyncMock()
    result = MagicMock(); result.scalar_one_or_none = lambda: model
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(IllegalStatusTransitionError):
        await BookingRepository().update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_not_awaited()
    async_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_extend_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    from datetime import datetime, timezone
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id, ttl_minutes=60, expires_at=datetime.now(timezone.utc))
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock(); result.scalar_one_or_none = lambda: model
    session.execute = AsyncMock(return_value=result)

    await BookingRepository().extend(session, model.id, 30, actor_id="u1")

    async_mock.assert_awaited_once_with(booking_id=model.id, environment_id=env_id)


@pytest.mark.asyncio
async def test_update_label_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    model.label = None
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock(); result.scalar_one_or_none = lambda: model
    session.execute = AsyncMock(return_value=result)

    await BookingRepository().update_label(session, model.id, "new-label", actor_id="u1")

    async_mock.assert_awaited_once_with(booking_id=model.id, environment_id=env_id)


# ── promote_next_queued / sync_promote_next_queued ──────────────────────────────
@pytest.mark.asyncio
async def test_promote_next_queued_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories import booking_repo as mod
    _, async_mock = mock_row_changed_publish

    env_id = uuid4()
    queued = SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240,
        static_vm_id=None, namespace_id=None, expires_at=None, environment_id=env_id,
    )
    free_vm = SimpleNamespace(id=uuid4(), name="vm-1")
    session = AsyncMock()
    session.add = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: free_vm
    session.execute = AsyncMock(side_effect=[r1, r2])
    session.refresh = AsyncMock()

    with patch.object(mod, "_to_entity", lambda m: m):
        await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)

    async_mock.assert_awaited_once_with(booking_id=queued.id, environment_id=env_id)


def test_sync_promote_next_queued_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories import booking_repo as mod
    sync_mock, _ = mock_row_changed_publish

    env_id = uuid4()
    queued = SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240,
        static_vm_id=None, namespace_id=None, expires_at=None, environment_id=env_id,
    )
    free_vm = SimpleNamespace(id=uuid4(), name="vm-1")
    session = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: free_vm
    session.execute = MagicMock(side_effect=[r1, r2])

    with patch.object(mod, "_to_entity", lambda m: m):
        mod.BookingRepository().sync_promote_next_queued(session, ResourceType.STATIC_VM.value)

    sync_mock.assert_called_once_with(booking_id=queued.id, environment_id=env_id)
