"""Tests for the CONFIGURING lifecycle state + provision-task config seam (v0.8.0 P1.1, #204).

The seam is a no-op today (`_needs_configuration` is always False), so existing VMs go
PROVISIONING → READY unchanged; when configuration is required the task enters CONFIGURING,
runs the injected config runner, then READY.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import Booking, HWConfig, User, VMImage
from app.domain.enums import BookingStatus, ResourceType


def _image(image_id):
    return VMImage(id=image_id, name="Ubuntu 22.04", vapp_template_id="tpl-1",
                   is_active=True, created_at=datetime.now(timezone.utc))


def _hw(hw_id):
    return HWConfig(id=hw_id, name="medium", cpus=2, memory_mb=4096, disk_mb=26624,
                    is_active=True, created_at=datetime.now(timezone.utc))


# ── Enum + classification ─────────────────────────────────────────────────────
def test_configuring_status_exists():
    assert BookingStatus.CONFIGURING.value == "CONFIGURING"


def test_configuring_is_force_deletable_and_in_flight():
    from app.application.use_cases import release_booking as rb
    assert BookingStatus.CONFIGURING in rb._FORCE_DELETABLE_STATUSES
    assert BookingStatus.CONFIGURING in rb._IN_FLIGHT_STATUSES
    # …but not directly releasable by an owner.
    assert BookingStatus.CONFIGURING not in rb._RELEASABLE_STATUSES


# ── Provision task seam ───────────────────────────────────────────────────────
def _run_provision():
    booking_id, image_id, hw_config_id = str(uuid4()), str(uuid4()), str(uuid4())
    mock_repo = MagicMock()
    # The post-apply booking fetch — no startup_script means the config seam is skipped.
    from types import SimpleNamespace
    mock_repo.sync_get = MagicMock(return_value=SimpleNamespace(startup_script=None, config_roles=[]))
    mock_image_repo = MagicMock(sync_get=MagicMock(return_value=_image(image_id)))
    mock_hw_repo = MagicMock(sync_get=MagicMock(return_value=_hw(hw_config_id)))
    return booking_id, image_id, hw_config_id, mock_repo, mock_image_repo, mock_hw_repo


def test_no_config_goes_straight_to_ready():
    bid, iid, hid, mock_repo, img, hw = _run_provision()
    with (
        patch("app.tasks.provision.SyncSessionLocal") as sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", img),
        patch("app.tasks.provision.hw_config_repo", hw),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.5"}),
        patch("app.tasks.provision.config_runner") as runner,
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[bid, iid, hid])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    # Stub mode has no real VM, so the reachability/config step is skipped entirely.
    assert BookingStatus.CONFIGURING not in statuses
    assert statuses[-1] == BookingStatus.READY
    runner.connect.assert_not_called()


def test_real_mode_enters_configuring_then_ready():
    """In real mode the worker waits for SSH (CONFIGURING) then marks READY."""
    bid, iid, hid, mock_repo, img, hw = _run_provision()
    with (
        patch("app.tasks.provision.SyncSessionLocal") as sf,
        patch("app.tasks.provision.repo", mock_repo),
        patch("app.tasks.provision.image_repo", img),
        patch("app.tasks.provision.hw_config_repo", hw),
        patch("app.tasks.provision.asyncio.run", return_value={"ip": "10.0.0.5"}),
        patch("app.tasks.provision.settings.USE_STUB_TERRAFORM", False),
        patch("app.tasks.provision.config_runner") as runner,
    ):
        sf.return_value.__enter__ = MagicMock(return_value=MagicMock())
        sf.return_value.__exit__ = MagicMock(return_value=False)
        runner.connect.return_value = MagicMock()
        from app.tasks.provision import provision_vm_task
        provision_vm_task.apply(args=[bid, iid, hid])

    statuses = [c.args[2] for c in mock_repo.sync_update_status.call_args_list]
    # PROVISIONING → CONFIGURING → READY, in order; reachability was checked.
    assert statuses == [BookingStatus.PROVISIONING, BookingStatus.CONFIGURING, BookingStatus.READY]
    runner.connect.assert_called_once()


# ── Release classification ────────────────────────────────────────────────────
def _booking(status, user_id="owner"):
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id=user_id, status=status, resource_type=ResourceType.VM,
        ttl_minutes=60, expires_at=now + timedelta(hours=1), created_at=now,
        image_id=uuid4(), image_name="Ubuntu", hw_config_id=uuid4(), hw_config_name="medium",
    )


def _user(role, uid="admin-id"):
    from uuid import UUID
    return User(id=UUID(int=abs(hash(uid)) % (10**38)), username=role, password_hash="",
                role=role, is_active=True, created_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_admin_force_deletes_configuring_booking():
    from app.application.use_cases.release_booking import ReleaseBookingUseCase
    from unittest.mock import AsyncMock

    booking = _booking(BookingStatus.CONFIGURING, user_id="someone")
    releasing = _booking(BookingStatus.RELEASING)
    releasing.id = booking.id
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[booking, releasing])
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()

    uc = ReleaseBookingUseCase(repo, dispatcher)
    result = await uc.execute(MagicMock(), booking.id, _user("admin"))

    assert result.status == BookingStatus.RELEASING
    dispatcher.dispatch_teardown.assert_called_once_with(str(booking.id))


@pytest.mark.asyncio
async def test_owner_cannot_normally_release_configuring():
    from app.application.use_cases.release_booking import ReleaseBookingUseCase
    from app.domain.exceptions import BookingError
    from unittest.mock import AsyncMock

    owner = _user("user", "owner-x")
    booking = _booking(BookingStatus.CONFIGURING, user_id=str(owner.id))
    repo = MagicMock()
    repo.get = AsyncMock(return_value=booking)
    repo.update_status = AsyncMock()
    dispatcher = MagicMock()

    uc = ReleaseBookingUseCase(repo, dispatcher)
    with pytest.raises(BookingError):
        await uc.execute(MagicMock(), booking.id, owner)
    dispatcher.dispatch_teardown.assert_not_called()
