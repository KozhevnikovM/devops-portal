"""Tests for #147 — disk quota is enforced per drive type (SSD vs HDD)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.domain.entities import HWConfig, VMImage
from app.domain.enums import DriveType
from app.domain.exceptions import QuotaExceededError


def _image():
    return VMImage(id=uuid4(), name="Ubuntu", vapp_template_id="tpl", is_active=True,
                   created_at=datetime.now(timezone.utc))


def _hw(drive_type: str, disk_gb: int = 100):
    return HWConfig(id=uuid4(), name="cfg", cpus=2, memory_mb=4096, disk_mb=disk_gb * 1024,
                    drive_type=drive_type, is_active=True, created_at=datetime.now(timezone.utc))


def _quota_repo(used, limits):
    repo = MagicMock()
    repo.count_active_resources = AsyncMock(return_value=used)
    repo.get_limits_for_update = AsyncMock(return_value=limits)
    return repo


def _use_case(hw, quota_repo):
    image_repo = MagicMock()
    image_repo.get = AsyncMock(return_value=_image())
    hw_repo = MagicMock()
    hw_repo.get = AsyncMock(return_value=hw)
    booking_repo = MagicMock()
    booking_repo.create = AsyncMock(side_effect=lambda s, b: b)
    return CreateBookingUseCase(booking_repo, image_repo, hw_repo, quota_repo)


_FULL_USED = {"cpus": 0, "memory_gb": 0, "ssd_gb": 0, "hdd_gb": 0}
_FULL_LIMITS = {"max_cpus": 100, "max_memory_gb": 1000, "max_ssd_gb": 50, "max_hdd_gb": 50}


@pytest.mark.asyncio
async def test_ssd_config_counts_toward_ssd_quota_and_is_rejected():
    hw = _hw(DriveType.SSD.value, disk_gb=100)  # exceeds max_ssd_gb=50
    uc = _use_case(hw, _quota_repo(dict(_FULL_USED), dict(_FULL_LIMITS)))
    with patch("app.application.use_cases.create_booking.provision_vm_task"):
        with pytest.raises(QuotaExceededError, match="SSD disk"):
            await uc.execute(AsyncMock(), ttl_minutes=60, image_id=uuid4(), hw_config_id=hw.id)


@pytest.mark.asyncio
async def test_hdd_config_unaffected_by_ssd_usage():
    # SSD is fully used, but an HDD config books fine — disk is tracked per type.
    hw = _hw(DriveType.HDD.value, disk_gb=40)  # within max_hdd_gb=50
    used = {"cpus": 0, "memory_gb": 0, "ssd_gb": 50, "hdd_gb": 0}
    uc = _use_case(hw, _quota_repo(used, dict(_FULL_LIMITS)))
    with patch("app.application.use_cases.create_booking.provision_vm_task"):
        booking = await uc.execute(AsyncMock(), ttl_minutes=60, image_id=uuid4(), hw_config_id=hw.id)
    assert booking.drive_type == DriveType.HDD.value
    assert booking.disk_mb == 40 * 1024


@pytest.mark.asyncio
async def test_ssd_config_snapshots_drive_type_on_success():
    hw = _hw(DriveType.SSD.value, disk_gb=10)  # within max_ssd_gb=50
    uc = _use_case(hw, _quota_repo(dict(_FULL_USED), dict(_FULL_LIMITS)))
    with patch("app.application.use_cases.create_booking.provision_vm_task"):
        booking = await uc.execute(AsyncMock(), ttl_minutes=60, image_id=uuid4(), hw_config_id=hw.id)
    assert booking.drive_type == DriveType.SSD.value
