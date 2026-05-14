import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.domain.enums import BookingStatus


def test_teardown_task_sets_released_status():
    """Teardown task transitions RELEASING → RELEASED on success."""
    booking_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.asyncio.run", return_value=None),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASING in statuses
    assert BookingStatus.RELEASED in statuses


def test_teardown_task_sets_failed_on_final_retry():
    """After all retries are exhausted, teardown sets FAILED."""
    booking_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.asyncio.run", side_effect=RuntimeError("destroy failed")),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASING in statuses
    assert BookingStatus.FAILED in statuses


def test_teardown_task_calls_destroy_with_workspace_id():
    """Teardown passes the correct workspace ID to terraform.destroy."""
    booking_id = str(uuid4())

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_terraform = MagicMock()
    mock_terraform.destroy = AsyncMock(return_value=None)

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.terraform", mock_terraform),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    mock_terraform.destroy.assert_called_once_with(f"booking-{booking_id}")


@pytest.mark.asyncio
async def test_stub_adapter_destroy_completes():
    """StubTerraformAdapter.destroy completes without error."""
    from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
    adapter = StubTerraformAdapter()
    with patch("asyncio.sleep", return_value=None):
        await adapter.destroy("booking-test-workspace")
