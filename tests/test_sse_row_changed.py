"""Regression tests for #388 — BookingRepository publishes a Redis row-changed notification
after every committed write that changes a booking row's rendered state, and never before/without
a commit. See docs/features/sse-live-updates.md.

#442: every publish also carries the booking's routing (owner/creator) and, for a lifecycle
publish of an environment child, the environment's own routing, read by primary key in a
separate short-lived session (PR #463 review) — never through the caller's session.

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


@pytest.fixture
def env_lookup():
    """Stub the environment routing lookups (async + sync); yields ``(async_mock, sync_mock)``."""
    from app.infrastructure.repositories import booking_repo as mod
    with patch.object(mod, "environment_routing", AsyncMock(return_value=ENV_ROUTING)) as a, \
            patch.object(mod, "sync_environment_routing", MagicMock(return_value=ENV_ROUTING)) as s:
        yield a, s


def _env_row_result(routing: Routing | None = ENV_ROUTING):
    """A ``session.execute`` result for the environment-routing query itself."""
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
def test_sync_update_status_publishes_after_commit(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    _, sync_lookup = env_lookup
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_called_once()
    # the lookup gets the caller's engine, not the caller's session
    sync_lookup.assert_called_once_with(session.get_bind.return_value, env_id)
    session.execute.assert_not_called()
    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


def test_standalone_lifecycle_publish_skips_environment_lookup(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    model = _model(created_by=DISPATCHER)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    env_lookup[1].assert_not_called()
    sync_mock.assert_called_once_with(**_lifecycle(
        model.id, booking_routing=Routing(owner_id=OWNER, created_by=DISPATCHER),
    ))


def test_failed_environment_lookup_still_publishes_and_keeps_the_write(
    mock_row_changed_publish, env_lookup,
):
    """Best-effort: the environment routing read failing after the commit must not fail the
    write; the notification goes out without environment routing (subscribers fall back). The
    caller's session is left alone — no rollback that would expire its instances (PR #463)."""
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_lookup[1].side_effect = ConnectionError("db hiccup")
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)  # no raise

    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, None))


class _ExpiringModel:
    """A booking whose attributes can no longer be read once 'expired' — standing in for what a
    rollback does to a real mapped instance (a refresh query that may fail, or MissingGreenlet)."""

    def __init__(self, **kw):
        object.__setattr__(self, "_values", _model(**kw).__dict__)
        object.__setattr__(self, "expired", False)

    def __getattr__(self, name):
        if object.__getattribute__(self, "expired"):
            raise AssertionError(f"read {name!r} from an expired instance")
        try:
            return object.__getattribute__(self, "_values")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_values")[name] = value


@pytest.mark.parametrize("lookup_fails", [True, False], ids=["lookup-fails", "lookup-succeeds"])
def test_booking_values_are_captured_before_the_environment_lookup(
    mock_row_changed_publish, env_lookup, lookup_fails,
):
    """PR #463 review: nothing about the booking may be read after the environment lookup starts,
    so no expiry it could cause can turn into another booking lookup (and a failure) before the
    publish."""
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _ExpiringModel(environment_id=env_id, created_by=DISPATCHER)
    session = MagicMock(); session.get.return_value = model

    def lookup(bind, environment_id):
        object.__setattr__(model, "expired", True)
        if lookup_fails:
            raise ConnectionError("db hiccup")
        return ENV_ROUTING
    env_lookup[1].side_effect = lookup

    BookingRepository().sync_update_status(session, model.id, BookingStatus.READY)

    sync_mock.assert_called_once_with(**_lifecycle(
        model._values["id"], env_id, None if lookup_fails else ENV_ROUTING,
        booking_routing=Routing(owner_id=OWNER, created_by=DISPATCHER),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_fails", [True, False], ids=["lookup-fails", "lookup-succeeds"])
async def test_async_booking_values_are_captured_before_the_environment_lookup(
    mock_row_changed_publish, env_lookup, lookup_fails,
):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _ExpiringModel(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=_scalar_result(model))

    async def lookup(bind, environment_id):
        object.__setattr__(model, "expired", True)
        if lookup_fails:
            raise ConnectionError("db hiccup")
        return ENV_ROUTING
    env_lookup[0].side_effect = lookup

    await BookingRepository().update_status(session, model._values["id"], BookingStatus.READY)

    session.rollback.assert_not_awaited()
    async_mock.assert_awaited_once_with(**_lifecycle(
        model._values["id"], env_id, None if lookup_fails else ENV_ROUTING,
    ))


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
def test_sync_set_status_message_publishes(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    sync_mock, _ = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = MagicMock(); session.get.return_value = model

    BookingRepository().sync_set_status_message(session, model.id, "hello")

    sync_mock.assert_called_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


def test_sync_record_progress_publishes_coalesced_progress(mock_row_changed_publish, env_lookup):
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
    env_lookup[1].assert_not_called()


# ── async update_status / extend / update_label ─────────────────────────────────
@pytest.mark.asyncio
async def test_async_update_status_publishes_after_commit(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    async_lookup, _ = env_lookup
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()  # sync method on the real AsyncSession
    session.execute = AsyncMock(return_value=_scalar_result(model))

    await BookingRepository().update_status(session, model.id, BookingStatus.READY)

    session.commit.assert_awaited_once()
    session.execute.assert_awaited_once()  # the booking load only — the lookup never uses it
    async_lookup.assert_awaited_once_with(session.bind, env_id)
    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


@pytest.mark.asyncio
async def test_async_failed_environment_lookup_still_publishes(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_lookup[0].side_effect = ConnectionError("db hiccup")
    env_id = uuid4()
    model = _model(environment_id=env_id)
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=_scalar_result(model))

    await BookingRepository().update_status(session, model.id, BookingStatus.READY)  # no raise

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
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
async def test_extend_publishes(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    from datetime import datetime, timezone
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id, ttl_minutes=60, expires_at=datetime.now(timezone.utc))
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=_scalar_result(model))

    await BookingRepository().extend(session, model.id, 30, actor_id="u1")

    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


@pytest.mark.asyncio
async def test_update_label_publishes(mock_row_changed_publish, env_lookup):
    from app.infrastructure.repositories.booking_repo import BookingRepository
    _, async_mock = mock_row_changed_publish
    env_id = uuid4()
    model = _model(environment_id=env_id)
    model.label = None
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=_scalar_result(model))

    await BookingRepository().update_label(session, model.id, "new-label", actor_id="u1")

    async_mock.assert_awaited_once_with(**_lifecycle(model.id, env_id, ENV_ROUTING))


# ── promote_next_queued / sync_promote_next_queued ──────────────────────────────
@pytest.mark.asyncio
async def test_promote_next_queued_publishes(mock_row_changed_publish, env_lookup):
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
    session.execute = AsyncMock(side_effect=[r1, r2])
    session.refresh = AsyncMock()

    with patch.object(mod, "_to_entity", lambda m: m):
        await mod.BookingRepository().promote_next_queued(session, ResourceType.STATIC_VM.value)

    async_mock.assert_awaited_once_with(**_lifecycle(queued.id, env_id, ENV_ROUTING))


def test_sync_promote_next_queued_publishes(mock_row_changed_publish, env_lookup):
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
    session.execute = MagicMock(side_effect=[r1, r2])

    with patch.object(mod, "_to_entity", lambda m: m):
        mod.BookingRepository().sync_promote_next_queued(session, ResourceType.STATIC_VM.value)

    sync_mock.assert_called_once_with(**_lifecycle(queued.id, env_id, ENV_ROUTING))


# ── environment routing lookup helpers (#442) ───────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("routing", [ENV_ROUTING, None], ids=["found", "missing"])
async def test_environment_routing_uses_its_own_short_lived_session(routing):
    from app.infrastructure.repositories import booking_repo as mod
    bind = MagicMock()
    own = AsyncMock(); own.execute = AsyncMock(return_value=_env_row_result(routing))
    cm = AsyncMock(); cm.__aenter__.return_value = own; cm.__aexit__.return_value = False

    with patch.object(mod, "AsyncSession", return_value=cm) as session_cls:
        assert await mod.environment_routing(bind, uuid4()) == routing

    session_cls.assert_called_once_with(bind)
    cm.__aexit__.assert_awaited_once()  # closed (connection released) before returning


@pytest.mark.parametrize("routing", [ENV_ROUTING, None], ids=["found", "missing"])
def test_sync_environment_routing_uses_its_own_short_lived_session(routing):
    from app.infrastructure.repositories import booking_repo as mod
    bind = MagicMock()
    own = MagicMock(); own.execute.return_value = _env_row_result(routing)
    cm = MagicMock(); cm.__enter__.return_value = own; cm.__exit__.return_value = False

    with patch.object(mod, "Session", return_value=cm) as session_cls:
        assert mod.sync_environment_routing(bind, uuid4()) == routing

    session_cls.assert_called_once_with(bind)
    cm.__exit__.assert_called_once()


def test_environment_routing_query_reads_the_environment_by_primary_key():
    from app.infrastructure.repositories.booking_repo import _environment_routing_stmt
    env_id = uuid4()
    sql = str(_environment_routing_stmt(env_id).compile(compile_kwargs={"literal_binds": True}))

    assert "FROM environments" in sql
    assert "environments.user_id" in sql and "environments.created_by" in sql
    assert env_id.hex in sql.replace("-", "")


# ── PR #462 review: adopted namespace in a dispatcher-ordered environment ────────
def test_adopted_namespace_publishes_booking_and_environment_routing_separately(
    mock_row_changed_publish, env_lookup,
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
    env_lookup[1].return_value = Routing(owner_id=OWNER, created_by=DISPATCHER)

    BookingRepository().sync_update_status(session, adopted.id, BookingStatus.RELEASING)

    sync_mock.assert_called_once_with(
        booking_id=adopted.id,
        booking_routing=Routing(owner_id=OWNER, created_by=None),
        environment_id=env_id,
        environment_routing=Routing(owner_id=OWNER, created_by=DISPATCHER),
    )
