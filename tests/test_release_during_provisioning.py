"""Regression tests for #394 — releasing an Environment/booking while its VM child is still
PROVISIONING used to race `terraform destroy` against the still-running `terraform apply` for the
same workspace, and then spin `provision_vm_task` into an illegal-transition retry storm once the
apply eventually returned. See docs/bugfix/394-release-during-provisioning-race.md.

Fix has three parts, one test group each:
  1. provision_vm_task holds a Redis "provisioning lock" for the duration of terraform.apply().
  2. teardown_vm_task (non-force) waits for that lock to clear before calling terraform.destroy().
  3. provision_vm_task bails out (queued-release case) or hands off to teardown (mid-apply-release
     case) instead of attempting an illegal status transition.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.domain.entities import HWConfig, VMImage
from app.domain.enums import BookingStatus


def _make_image():
    return VMImage(id=uuid4(), name="Ubuntu", vapp_template_id="tpl-1",
                   is_active=True, created_at=datetime.now(timezone.utc))


def _make_hw():
    return HWConfig(id=uuid4(), name="medium", cpus=2, memory_mb=4096, disk_mb=26624,
                    is_active=True, created_at=datetime.now(timezone.utc))


# ── provision_vm_task: holds a provisioning lock across terraform.apply() ────────────────
def test_provisioning_lock_acquired_and_released_around_apply():
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=MagicMock(status=BookingStatus.PENDING))
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_make_image()))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_make_hw()))
    mock_lock = MagicMock()
    mock_lock.get_client.return_value = "fake-redis-client"

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", ""),
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.provisioning_lock", mock_lock),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    mock_lock.acquire.assert_called_once_with("fake-redis-client", booking_id)
    mock_lock.release.assert_called_once_with("fake-redis-client", booking_id)


def test_provisioning_lock_released_even_if_apply_raises():
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=MagicMock(status=BookingStatus.PENDING))
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_make_image()))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_make_hw()))
    mock_lock = MagicMock()
    mock_lock.get_client.return_value = "fake-redis-client"

    with (
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.settings.VCD_API_TOKENS", ""),
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.provisioning_lock", mock_lock),
        patch("app.tasks.provision.asyncio.run", side_effect=RuntimeError("VCD error")),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    mock_lock.release.assert_called_once_with("fake-redis-client", booking_id)


def test_provisioning_lock_skipped_under_stub_adapter():
    """Stub mode has no real terraform state lock to race, so no lock is taken."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=MagicMock(status=BookingStatus.PENDING))
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_make_image()))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_make_hw()))
    mock_lock = MagicMock()

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.provisioning_lock", mock_lock),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    mock_lock.get_client.assert_not_called()
    mock_lock.acquire.assert_not_called()


# ── provision_vm_task: bail out / hand off when released around it ──────────────────────
def test_provision_task_noops_when_booking_already_released_at_start():
    """Booking was released while this task sat queued or waited a RETRY backoff — starting
    provisioning now would attempt an illegal RELEASING/RELEASED -> PROVISIONING transition."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=MagicMock(status=BookingStatus.RELEASING))
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_make_image()))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_make_hw()))

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run") as mock_asyncio_run,
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        result = provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    assert result.successful()
    mock_repo.sync_update_status.assert_not_called()
    mock_asyncio_run.assert_not_called()


def test_provision_task_hands_off_to_teardown_when_released_mid_apply():
    """Booking was released while terraform.apply() was actually running — provision_vm_task must
    hand off to teardown_vm_task instead of trying to configure/ready a VM about to be destroyed."""
    booking_id = str(uuid4())
    image_id = str(uuid4())
    hw_config_id = str(uuid4())

    # First sync_get (pre-apply "existing" check) sees PENDING; second (post-apply) sees the
    # booking was released while apply was in flight.
    statuses = [BookingStatus.PENDING, BookingStatus.RELEASING]

    def _fake_sync_get(*_args, **_kwargs):
        return MagicMock(status=statuses.pop(0), vm_password=None,
                          startup_script=None, config_roles=None)

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(side_effect=_fake_sync_get)
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_make_image()))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_make_hw()))

    with (
        patch("app.tasks.provision.SyncSessionLocal") as mock_sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", mock_image_repo),
        patch("app.tasks.provision.hw_config_repo", mock_hw_repo),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.1"}),
        patch("app.tasks.provision.teardown_vm_task") as mock_teardown,
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.provision import provision_vm_task
        result = provision_vm_task.apply(args=[booking_id, image_id, hw_config_id])

    assert result.successful()
    mock_teardown.delay.assert_called_once_with(booking_id, request_id=None)

    statuses_written = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.CONFIGURING not in statuses_written
    assert BookingStatus.READY not in statuses_written


# ── teardown_vm_task: waits for an in-flight apply before destroying ────────────────────
def test_teardown_waits_for_provisioning_lock_before_destroy():
    booking_id = str(uuid4())
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(vapp_template_id="tpl")))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=26624)))

    # Held for the first two checks, then clears.
    mock_lock = MagicMock()
    mock_lock.get_client.return_value = "fake-redis-client"
    mock_lock.is_held.side_effect = [True, True, False]

    with (
        patch("app.tasks.teardown.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.teardown.SyncSessionLocal") as mock_sf,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.provisioning_lock", mock_lock),
        patch("app.tasks.teardown.time.sleep", return_value=None),
        patch("app.tasks.teardown.asyncio.run", return_value=None) as mock_asyncio_run,
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    assert mock_lock.is_held.call_count == 3
    mock_asyncio_run.assert_called_once()

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    assert BookingStatus.RELEASED in statuses


def test_teardown_does_not_wait_when_force():
    """Force teardown (admin force-release, restart recovery) pushes through immediately —
    it must not block on a possibly-stale lock left by a dead process."""
    booking_id = str(uuid4())
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(vapp_template_id="tpl")))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=26624)))
    mock_lock = MagicMock()

    with (
        patch("app.tasks.teardown.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.teardown.SyncSessionLocal") as mock_sf,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.provisioning_lock", mock_lock),
        patch("app.tasks.teardown.asyncio.run", return_value=None),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id], kwargs={"force": True})

    mock_lock.is_held.assert_not_called()


def test_teardown_does_not_wait_under_stub_adapter():
    booking_id = str(uuid4())
    mock_booking = MagicMock()
    mock_booking.image_id = uuid4()
    mock_booking.hw_config_id = uuid4()

    mock_repo = MagicMock()
    mock_repo.sync_get = MagicMock(return_value=mock_booking)
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(vapp_template_id="tpl")))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=MagicMock(cpus=2, memory_mb=4096, disk_mb=26624)))
    mock_lock = MagicMock()

    with (
        patch("app.tasks.teardown.SyncSessionLocal") as mock_sf,
        patch("app.tasks.teardown.repo", mock_repo),
        patch("app.tasks.teardown.image_repo", mock_image_repo),
        patch("app.tasks.teardown.hw_config_repo", mock_hw_repo),
        patch("app.tasks.teardown.provisioning_lock", mock_lock),
        patch("app.tasks.teardown.asyncio.run", return_value=None),
    ):
        mock_sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_sf.return_value.__exit__ = MagicMock(return_value=False)

        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[booking_id])

    mock_lock.is_held.assert_not_called()
