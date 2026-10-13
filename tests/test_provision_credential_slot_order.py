"""Real-adapter regression test for credential-slot acquisition order (PR #519, #429 Decision 9).

Verifies that when all VCD token slots are busy in real-adapter mode:
1. A booking already in RELEASING or RELEASED is checked before waiting for a slot,
   so it never attempts Redis lock acquisition, never delays, and never calls self.retry.
2. A PENDING booking checks the slot, fails to acquire, and raises Retry with no status write.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.exceptions import Retry

from app.domain.enums import BookingStatus
from app.tasks.provision import provision_vm_task


@pytest.mark.parametrize("status", [BookingStatus.RELEASED, BookingStatus.RELEASING])
def test_released_booking_never_attempts_slot_acquisition(status: BookingStatus):
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get.return_value = MagicMock(
        id=booking_id,
        status=status,
        vm_password=None,
    )

    mock_redis = MagicMock()
    mock_redis.set.return_value = False  # slots always busy

    curr_time = [0.0]

    def advance_on_sleep(_):
        curr_time[0] += 100.0

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "vcd-token-1"),
        patch("app.tasks.provision.settings.VCD_TOKEN_MAX_PARALLEL", 1),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.time.monotonic", side_effect=lambda: curr_time[0]),
        patch("app.tasks.provision.time.sleep", side_effect=advance_on_sleep),
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.SyncSessionLocal"),
    ):
        # Must return cleanly without raising Retry or any exception
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    # No Redis set calls made
    mock_redis.set.assert_not_called()
    # No status writes or message writes
    mock_repo.sync_update_status.assert_not_called()
    mock_repo.sync_set_status_message.assert_not_called()


def test_pending_booking_reschedules_when_slots_busy():
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get.return_value = MagicMock(
        id=booking_id,
        status=BookingStatus.PENDING,
        vm_password=None,
    )

    mock_redis = MagicMock()
    mock_redis.set.return_value = False  # slots always busy

    curr_time = [0.0]

    def advance_on_sleep(_):
        curr_time[0] += 100.0

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "vcd-token-1"),
        patch("app.tasks.provision.settings.VCD_TOKEN_MAX_PARALLEL", 1),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.time.monotonic", side_effect=lambda: curr_time[0]),
        patch("app.tasks.provision.time.sleep", side_effect=advance_on_sleep),
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.SyncSessionLocal"),
        pytest.raises(Retry),
    ):
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id], throw=True)

    # Slot acquisition was attempted on Redis
    assert mock_redis.set.called
    # No booking writes were performed
    mock_repo.sync_update_status.assert_not_called()
    mock_repo.sync_set_status_message.assert_not_called()
