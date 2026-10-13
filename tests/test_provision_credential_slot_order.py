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


def test_pre_slot_guard_db_failure_retries_task():
    """Regression: Pre-slot guard DB failure retries task without attempting slot acquisition."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get.side_effect = RuntimeError("DB disconnect during pre-slot check")

    mock_redis = MagicMock()

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "vcd-token-1"),
        patch("app.tasks.provision.settings.VCD_TOKEN_MAX_PARALLEL", 1),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.SyncSessionLocal"),
        pytest.raises(Retry),
    ):
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id], throw=True)

    # Slot acquisition was NEVER attempted on Redis
    mock_redis.set.assert_not_called()
    # Best-effort failure writes were attempted
    mock_repo.sync_update_status.assert_called_once()
    assert mock_repo.sync_update_status.call_args[0][2] == BookingStatus.RETRY


def test_post_slot_guard_db_failure_retries_task_and_releases_slot():
    """Regression: Post-slot guard DB failure retries task and releases acquired slot."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get.side_effect = [
        MagicMock(id=booking_id, status=BookingStatus.PENDING, vm_password=None),
        RuntimeError("DB disconnect during post-slot check"),
    ]

    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "vcd-token-1"),
        patch("app.tasks.provision.settings.VCD_TOKEN_MAX_PARALLEL", 1),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.SyncSessionLocal"),
        pytest.raises(Retry),
    ):
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id], throw=True)

    # Slot acquisition was attempted on Redis and released
    mock_redis.set.assert_called_once()
    mock_redis.delete.assert_called_once()
    # Best-effort failure writes were attempted
    mock_repo.sync_update_status.assert_called_once()
    assert mock_repo.sync_update_status.call_args[0][2] == BookingStatus.RETRY


def test_slot_release_failure_after_successful_provision_does_not_retry_task():
    """Regression: Redis slot-release failure after successful provisioning does not retry task."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())
    fake_ip = "10.0.0.42"

    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    # Simulate Redis connection failure when releasing the slot
    mock_redis.delete.side_effect = RuntimeError("Redis connection error on delete")

    mock_session = MagicMock()
    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(
        return_value=MagicMock(
            status=BookingStatus.PENDING,
            vm_password=None,
            startup_script=None,
            config_roles=None,
        )
    )
    mock_image_repo = MagicMock()
    mock_image_repo.sync_get = MagicMock(
        return_value=MagicMock(vapp_template_id="tpl-1")
    )
    mock_hw_repo = MagicMock()
    mock_hw_repo.sync_get = MagicMock(
        return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=20480)
    )

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", "vcd-token-1"),
        patch("app.tasks.provision.settings.VCD_TOKEN_MAX_PARALLEL", 1),
        patch("app.tasks.provision.redis_lib.Redis.from_url", return_value=mock_redis),
        patch("app.tasks.provision.SyncSessionLocal") as mock_session_factory,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": fake_ip}),
    ):
        mock_session_factory.return_value.__enter__ = MagicMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

        # Must NOT raise Retry!
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id], throw=True)

    # Slot acquisition and release were both attempted on Redis
    mock_redis.set.assert_called_once()
    mock_redis.delete.assert_called_once()
    # Booking reached READY, never RETRY or FAILED
    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.READY in statuses
    assert BookingStatus.RETRY not in statuses
    assert BookingStatus.FAILED not in statuses
