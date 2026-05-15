from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.domain.enums import BookingStatus


def _make_booking(status=BookingStatus.PROVISIONING):
    b = MagicMock()
    b.id = uuid4()
    b.status = status
    return b


# ---------------------------------------------------------------------------
# skip when stub
# ---------------------------------------------------------------------------

def test_recovery_skips_when_stub_terraform():
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.SyncSessionLocal") as mock_session_cls,
    ):
        mock_settings.USE_STUB_TERRAFORM = True

        from app.main import _recover_in_progress_bookings
        _recover_in_progress_bookings()

        mock_session_cls.assert_not_called()


# ---------------------------------------------------------------------------
# no-op when no in-progress bookings
# ---------------------------------------------------------------------------

def test_recovery_no_op_when_no_in_progress_bookings():
    mock_repo = MagicMock()
    mock_repo.sync_list_in_progress.return_value = []
    mock_task = MagicMock()

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.BookingRepository", return_value=mock_repo),
        patch("app.main.provision_vm_task", mock_task),
        patch("app.main.SyncSessionLocal") as mock_session_cls,
    ):
        mock_settings.USE_STUB_TERRAFORM = False
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        from app.main import _recover_in_progress_bookings
        _recover_in_progress_bookings()

    mock_task.delay.assert_not_called()


# ---------------------------------------------------------------------------
# re-queues one task per in-progress booking
# ---------------------------------------------------------------------------

def test_recovery_requeues_task_for_each_in_progress_booking():
    bookings = [
        _make_booking(BookingStatus.PENDING),
        _make_booking(BookingStatus.PROVISIONING),
        _make_booking(BookingStatus.RETRY),
    ]
    mock_repo = MagicMock()
    mock_repo.sync_list_in_progress.return_value = bookings
    mock_task = MagicMock()

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.BookingRepository", return_value=mock_repo),
        patch("app.main.provision_vm_task", mock_task),
        patch("app.main.SyncSessionLocal") as mock_session_cls,
    ):
        mock_settings.USE_STUB_TERRAFORM = False
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        from app.main import _recover_in_progress_bookings
        _recover_in_progress_bookings()

    assert mock_task.delay.call_count == 3
    mock_task.delay.assert_any_call(str(bookings[0].id))
    mock_task.delay.assert_any_call(str(bookings[1].id))
    mock_task.delay.assert_any_call(str(bookings[2].id))


# ---------------------------------------------------------------------------
# uses sync_list_in_progress (no age filter, unlike stale_provisioning)
# ---------------------------------------------------------------------------

def test_recovery_uses_list_in_progress_not_stale():
    booking = _make_booking(BookingStatus.PROVISIONING)
    mock_repo = MagicMock()
    mock_repo.sync_list_in_progress.return_value = [booking]
    mock_task = MagicMock()

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.BookingRepository", return_value=mock_repo),
        patch("app.main.provision_vm_task", mock_task),
        patch("app.main.SyncSessionLocal") as mock_session_cls,
    ):
        mock_settings.USE_STUB_TERRAFORM = False
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        from app.main import _recover_in_progress_bookings
        _recover_in_progress_bookings()

    mock_repo.sync_list_in_progress.assert_called_once()
    mock_repo.sync_list_stale_provisioning.assert_not_called()
