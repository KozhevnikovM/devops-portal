from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from app.domain.enums import BookingStatus


def _make_booking(status=BookingStatus.READY, age_minutes=0):
    b = MagicMock()
    b.id = uuid4()
    b.status = status
    b.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    b.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return b


# ---------------------------------------------------------------------------
# enforce_ttl
# ---------------------------------------------------------------------------

def test_enforce_ttl_queues_teardown_for_each_expired_booking():
    bookings = [_make_booking(), _make_booking()]
    mock_repo = MagicMock()
    mock_repo.sync_list_expired.return_value = bookings

    mock_task = MagicMock()

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.teardown_vm_task", mock_task),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import enforce_ttl
        enforce_ttl()

    assert mock_task.delay.call_count == 2
    mock_task.delay.assert_any_call(str(bookings[0].id))
    mock_task.delay.assert_any_call(str(bookings[1].id))


def test_enforce_ttl_sets_releasing_before_queuing():
    booking = _make_booking()
    mock_repo = MagicMock()
    mock_repo.sync_list_expired.return_value = [booking]

    mock_task = MagicMock()
    call_order = []
    mock_repo.sync_update_status.side_effect = lambda s, bid, status: call_order.append(("update", status))
    mock_task.delay.side_effect = lambda bid: call_order.append(("delay", bid))

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.teardown_vm_task", mock_task),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import enforce_ttl
        enforce_ttl()

    assert call_order[0] == ("update", BookingStatus.RELEASING)
    assert call_order[1][0] == "delay"


def test_enforce_ttl_skips_non_expired_bookings():
    mock_repo = MagicMock()
    mock_repo.sync_list_expired.return_value = []

    mock_task = MagicMock()

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.teardown_vm_task", mock_task),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import enforce_ttl
        enforce_ttl()

    mock_task.delay.assert_not_called()


def test_enforce_ttl_continues_after_one_booking_fails():
    b1 = _make_booking()
    b2 = _make_booking()
    mock_repo = MagicMock()
    mock_repo.sync_list_expired.return_value = [b1, b2]
    mock_repo.sync_update_status.side_effect = [RuntimeError("db error"), None]

    mock_task = MagicMock()

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.teardown_vm_task", mock_task),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import enforce_ttl
        enforce_ttl()  # must not raise

    # b1 failed on status update, b2 should still be queued
    mock_task.delay.assert_called_once_with(str(b2.id))


# ---------------------------------------------------------------------------
# reap_stale_provisioning
# ---------------------------------------------------------------------------

def test_reap_stale_provisioning_marks_stale_bookings_failed():
    b1 = _make_booking(status=BookingStatus.PROVISIONING, age_minutes=120)
    b2 = _make_booking(status=BookingStatus.PENDING, age_minutes=90)
    mock_repo = MagicMock()
    mock_repo.sync_list_stale_provisioning.return_value = [b1, b2]

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import reap_stale_provisioning
        reap_stale_provisioning()

    statuses_set = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert all(s == BookingStatus.FAILED for s in statuses_set)
    assert len(statuses_set) == 2


def test_reap_stale_provisioning_skips_under_threshold():
    mock_repo = MagicMock()
    mock_repo.sync_list_stale_provisioning.return_value = []

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import reap_stale_provisioning
        reap_stale_provisioning()

    mock_repo.sync_update_status.assert_not_called()


def test_reap_stale_provisioning_continues_after_one_booking_fails():
    b1 = _make_booking(status=BookingStatus.PROVISIONING, age_minutes=120)
    b2 = _make_booking(status=BookingStatus.RETRY, age_minutes=120)
    mock_repo = MagicMock()
    mock_repo.sync_list_stale_provisioning.return_value = [b1, b2]
    mock_repo.sync_update_status.side_effect = [RuntimeError("db error"), None]

    with (
        patch("app.tasks.beat_tasks.repo", mock_repo),
        patch("app.tasks.beat_tasks.SyncSessionLocal") as mock_session_factory,
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.beat_tasks import reap_stale_provisioning
        reap_stale_provisioning()  # must not raise

    assert mock_repo.sync_update_status.call_count == 2
