"""Regression tests for #388 — BookingRepository publishes a Redis row-changed notification
after every committed write that changes a booking row's rendered state, and never before/without
a commit. See docs/features/sse-live-updates.md.

#442: every publish also carries the booking's routing (owner/creator) and, for a lifecycle
publish of an environment child, the environment's own routing, read by primary key.

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
from app.infrastructure.events import Routing

OWNER, DISPATCHER = "owner-1", "dispatcher-1"
BOOKING_ROUTING = Routing(owner_id=OWNER, created_by=None)
ENV_ROUTING = Routing(owner_id=OWNER, created_by=DISPATCHER)


def _model(**kw):
    defaults = dict(
        id=uuid4(), status=BookingStatus.PROVISIONING.value, environment_id=None,
        vm_ip=None, vm_password=None, config_failed=False, status_message=None,
        provisioning_log=None, ttl_minutes=240, expires_at=None,
        user_id=OWNER, created_by=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _env_row_result(routing: Routing | None = ENV_ROUTING):
    """A ``session.execute`` result for the environment-routing lookup."""
    row = None if routing is None else SimpleNamespace(
        user_id=routing.owner_id, created_by=routing.created_by,
    )
    result = MagicMock(); result.one_or_none = lambda: row
    return result


def _scalar_result(model):
    result = MagicMock(); result.scalar_one_or_none = lambda: model
    return result


def _lifecycle(booking_id, env_id=None, env_routing=None, booking_routing=BOOKING_ROUTING):
    return {
        "booking_id": booking_id, "booking_routing": booking_routing,
        "environment_id": env_id, "environment_routing": env_routing,
    }


# ── sync_update_status ───────────────────────────────────────────────────────────
def test_sync_update_status_publishes_after_commit(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model
    session.execute.return_value = _env_row_result()

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_called_once()
    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


def test_standalone_lifecycle_publish_skips_environment_lookup(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    model = _model(created_by=DISPATCHER)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    session.execute.assert_not_called()
    sync_mock.assert_called_once_with(**_lifecycle(
        model.id, booking_routing=Routing(owner_id=OWNER, created_by=DISPATCHER),
    ))


def test_failed_environment_lookup_still_publishes_and_keeps_the_write(mock_row_changed_publish):
    """Best-effort: the environment routing read failing after the commit must not fail the
    write; the notification goes out without environment routing (subscribers fall back)."""
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model
    session.execute.side_effect = ConnectionError("db hiccup")

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)  # no raise

    session.commit.assert_called_once()
    session.rollback.assert_called_once()
    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, None))


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
    session.execute.return_value = _env_row_result()

    BookingRepository().sync_set_status_message(session, model.id, "hello")

    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


def test_sync_record_progress_publishes_coalesced_progress(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_record_progress(session, model.id, "log line")

    # Progress goes through the coalesced path (#440), never the immediate lifecycle one — and
    # never pays for the environment routing lookup: it doesn't refresh the environment row (#442).
    session.commit.assert_called_once()
    sync_mock.progress.assert_called_once_with(
        booking_id=model.id, booking_routing=BOOKING_ROUTING, environment_id=env_id,
    )
    sync_mock.assert_not_called()
    session.execute.assert_not_called()


# ── async update_status / extend / update_label ─────────────────────────────────
@pytest.mark.asyncio
async def test_async_update_status_publishes_after_commit(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()  # sync method on the real AsyncSession
    session.execute = AsyncMock(side_effect=[_scalar_result(model), _env_row_result()])

    await BookingRepository().update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_awaited_once()
    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


@pytest.mark.asyncio
async def test_async_failed_environment_lookup_still_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(side_effect=[_scalar_result(model), ConnectionError("db hiccup")])

    await BookingRepository().update_status(session, model.id, BookingStatus.READY)  # no raise

    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()
    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, None))


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
    session.execute = AsyncMock(side_effect=[_scalar_result(model), _env_row_result()])

    await BookingRepository().extend(session, model.id, 30, actor_id="u1")

    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


@pytest.mark.asyncio
async def test_update_label_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    model.label = None
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(side_effect=[_scalar_result(model), _env_row_result()])

    await BookingRepository().update_label(session, model.id, "new-label", actor_id="u1")

    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


# ── promote_next_queued / sync_promote_next_queued ──────────────────────────────
@pytest.mark.asyncio
async def test_promote_next_queued_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories import booking_repo as mod
    _, async_mock = mock_row_changed_publish

    env_id = uuid4()
    queued = SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240,
        static_vm_id=None, namespace_id=None, expires_at=None, environment_id=env_id,
        user_id=OWNER, created_by=None,
    )
    free_vm = SimpleNamespace(id=uuid4(), name="vm-1")
    session = AsyncMock()
    session.add = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: free_vm
    session.execute = AsyncMock(side_effect=[r1, r2, _env_row_result()])
    session.refresh = AsyncMock()

    with patch.object(mod, "_to_entity", lambda m: m):
        await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)

    async_mock.assert_awaited_once_with(**_lifecycle(queued.id, env_id, ENV_ROUTING))


def test_sync_promote_next_queued_publishes(mock_row_changed_publish):
    from app.infrastructure.repositories import booking_repo as mod
    sync_mock, _ = mock_row_changed_publish

    env_id = uuid4()
    queued = SimpleNamespace(
        id=uuid4(), status=BookingStatus.QUEUED.value, ttl_minutes=240,
        static_vm_id=None, namespace_id=None, expires_at=None, environment_id=env_id,
        user_id=OWNER, created_by=None,
    )
    free_vm = SimpleNamespace(id=uuid4(), name="vm-1")
    session = MagicMock()
    r1 = MagicMock(); r1.scalar_one_or_none = lambda: queued
    r2 = MagicMock(); r2.scalar_one_or_none = lambda: free_vm
    session.execute = MagicMock(side_effect=[r1, r2, _env_row_result()])

    with patch.object(mod, "_to_entity", lambda m: m):
        mod.BookingRepository().sync_promote_next_queued(session, ResourceType.STATIC_VM.value)

    sync_mock.assert_called_once_with(**_lifecycle(queued.id, env_id, ENV_ROUTING))


# ── environment routing lookup helpers (#442) ───────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("routing", [ENV_ROUTING, None], ids=["found", "missing"])
async def test_environment_routing(routing):
    from app.infrastructure.repositories.booking_repo import environment_routing
    session = AsyncMock(); session.execute = AsyncMock(return_value=_env_row_result(routing))

    assert await environment_routing(session, uuid4()) == routing


@pytest.mark.parametrize("routing", [ENV_ROUTING, None], ids=["found", "missing"])
def test_sync_environment_routing(routing):
    from app.infrastructure.repositories.booking_repo import sync_environment_routing
    session = MagicMock(); session.execute.return_value = _env_row_result(routing)

    assert sync_environment_routing(session, uuid4()) == routing


def test_environment_routing_query_reads_the_environment_by_primary_key():
    from app.infrastructure.repositories.booking_repo import _environment_routing_stmt
    env_id = uuid4()
    sql = str(_environment_routing_stmt(env_id).compile(compile_kwargs={"literal_binds": True}))

    assert "FROM environments" in sql
    assert "environments.user_id" in sql and "environments.created_by" in sql
    assert env_id.hex in sql.replace("-", "")


# ── PR #462 review: adopted namespace in a dispatcher-ordered environment ────────
def test_adopted_namespace_publishes_booking_and_environment_routing_separately(
    mock_row_changed_publish,
):
    """User U's standalone namespace booking (no creator) adopted into an environment dispatcher D
    ordered for U: set_environment() keeps the booking's created_by, the environment records D.
    The lifecycle publish must carry both, unmerged."""
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    adopted = _model(
        environment_id=env_id, status=BookingStatus.READY.value, user_id=OWNER, created_by=None,
    )
    session = MagicMock(); session.get.return_value = adopted
    session.execute.return_value = _env_row_result(Routing(owner_id=OWNER, created_by=DISPATCHER))

    BookingRepository().sync_update_status(session, adopted.id, BookingStatus.RELEASING)

    sync_mock.assert_called_once_with(
        booking_id=adopted.id,
        booking_routing=Routing(owner_id=OWNER, created_by=None),
        environment_id=env_id,
        environment_routing=Routing(owner_id=OWNER, created_by=DISPATCHER),
    )
