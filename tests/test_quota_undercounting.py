"""Regression tests for #293: quota under-counting — floor division (D3) + CONFIGURING invisible (D4)."""
import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.domain.entities import HWConfig, VMImage
from app.domain.enums import BookingStatus, DriveType
from app.domain.exceptions import QuotaExceededError
from app.infrastructure.repositories.quota_repo import _ACTIVE_STATUSES


# ── D4: CONFIGURING must be in _ACTIVE_STATUSES ──────────────────────────────


def test_configuring_included_in_active_statuses():
    """CONFIGURING was missing, letting users race bookings past quota during Ansible config."""
    assert BookingStatus.CONFIGURING.value in _ACTIVE_STATUSES


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_hw(memory_mb: int, disk_mb: int = 20480, cpus: int = 2, drive_type=DriveType.HDD) -> HWConfig:
    return HWConfig(
        id=uuid4(), name="test", cpus=cpus, memory_mb=memory_mb, disk_mb=disk_mb,
        is_active=True, created_at=datetime.now(timezone.utc),
        drive_type=drive_type.value if hasattr(drive_type, "value") else drive_type,
    )


def _make_use_case(hw: HWConfig, *, used: dict, limits: dict) -> tuple[CreateBookingUseCase, MagicMock]:
    image = VMImage(id=uuid4(), name="Ubuntu", vapp_template_id="t", is_active=True,
                    created_at=datetime.now(timezone.utc))
    image_repo = MagicMock(get=AsyncMock(return_value=image))
    hw_repo = MagicMock(get=AsyncMock(return_value=hw))
    booking_repo = MagicMock()
    booking_repo.create = AsyncMock(side_effect=lambda s, b: b)
    quota_repo = MagicMock()
    quota_repo.get_limits_for_update = AsyncMock(return_value=limits)
    quota_repo.count_active_resources = AsyncMock(return_value=used)
    dispatcher = MagicMock()
    uc = CreateBookingUseCase(booking_repo, image_repo, hw_repo, quota_repo, dispatcher)
    return uc, quota_repo


# ── D3: 512 MB VM must cost 1 GB memory quota (ceil), not 0 (floor) ──────────


@pytest.mark.asyncio
async def test_512mb_vm_costs_1gb_memory_quota():
    """512 MB floors to 0 with //, letting a 512 MB booking slip past a 1 GB limit.

    With ceil, 512 MB → 1 GB; if the user already has 1 GB used, the booking is rejected.
    """
    hw = _make_hw(memory_mb=512)
    uc, _ = _make_use_case(
        hw,
        used={"cpus": 0, "memory_gb": 1, "hdd_gb": 0, "ssd_gb": 0},
        limits={"max_cpus": 100, "max_memory_gb": 1, "max_hdd_gb": 100, "max_ssd_gb": 100},
    )
    with pytest.raises(QuotaExceededError, match="memory"):
        await uc.execute(MagicMock(), 60, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_512mb_vm_allowed_when_quota_has_headroom():
    """A 512 MB VM (→ 1 GB) is allowed when 1 GB headroom remains."""
    hw = _make_hw(memory_mb=512)
    uc, _ = _make_use_case(
        hw,
        used={"cpus": 0, "memory_gb": 0, "hdd_gb": 0, "ssd_gb": 0},
        limits={"max_cpus": 100, "max_memory_gb": 1, "max_hdd_gb": 100, "max_ssd_gb": 100},
    )
    # Should not raise — 0 + ceil(512/1024)=1 is not > 1
    await uc.execute(MagicMock(), 60, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_1536mb_vm_costs_2gb_memory_quota():
    """1536 MB floors to 1 GB with //, but ceils to 2 GB — with a 1 GB limit it must be rejected."""
    hw = _make_hw(memory_mb=1536)
    uc, _ = _make_use_case(
        hw,
        used={"cpus": 0, "memory_gb": 0, "hdd_gb": 0, "ssd_gb": 0},
        limits={"max_cpus": 100, "max_memory_gb": 1, "max_hdd_gb": 100, "max_ssd_gb": 100},
    )
    with pytest.raises(QuotaExceededError, match="memory"):
        await uc.execute(MagicMock(), 60, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_memory_cost_matches_aggregate_ceil():
    """Verify the computed memory cost equals math.ceil(memory_mb / 1024) for various sizes."""
    for memory_mb, expected_gb in [(512, 1), (1024, 1), (1536, 2), (2048, 2), (3072, 3)]:
        hw = _make_hw(memory_mb=memory_mb)
        # used=0, limit=expected_gb → 0 + expected_gb is NOT > expected_gb → PASS.
        uc, _ = _make_use_case(
            hw,
            used={"cpus": 0, "memory_gb": 0, "hdd_gb": 0, "ssd_gb": 0},
            limits={"max_cpus": 100, "max_memory_gb": expected_gb, "max_hdd_gb": 100, "max_ssd_gb": 100},
        )
        await uc.execute(MagicMock(), 60, uuid4(), uuid4())

        # used=expected_gb, same limit → expected_gb + expected_gb > expected_gb → FAIL.
        uc2, _ = _make_use_case(
            hw,
            used={"cpus": 0, "memory_gb": expected_gb, "hdd_gb": 0, "ssd_gb": 0},
            limits={"max_cpus": 100, "max_memory_gb": expected_gb, "max_hdd_gb": 100, "max_ssd_gb": 100},
        )
        with pytest.raises(QuotaExceededError):
            await uc2.execute(MagicMock(), 60, uuid4(), uuid4())


# ── D3: disk uses ceil too ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_512mb_disk_costs_1gb_disk_quota():
    """512 MB disk floors to 0 GB with //, leaking past a 1 GB HDD limit."""
    hw = _make_hw(memory_mb=4096, disk_mb=512)
    uc, _ = _make_use_case(
        hw,
        used={"cpus": 0, "memory_gb": 0, "hdd_gb": 1, "ssd_gb": 0},
        limits={"max_cpus": 100, "max_memory_gb": 100, "max_hdd_gb": 1, "max_ssd_gb": 100},
    )
    with pytest.raises(QuotaExceededError, match="HDD"):
        await uc.execute(MagicMock(), 60, uuid4(), uuid4())


# ── D4: CONFIGURING booking counted in quota check ───────────────────────────


@pytest.mark.asyncio
async def test_booking_rejected_when_configuring_vm_fills_quota():
    """count_active_resources now includes CONFIGURING; a second booking should be blocked."""
    hw = _make_hw(memory_mb=4096)  # 4 GB
    uc, _ = _make_use_case(
        hw,
        # Simulate a CONFIGURING booking already consuming the full 4 GB quota.
        used={"cpus": 2, "memory_gb": 4, "hdd_gb": 0, "ssd_gb": 0},
        limits={"max_cpus": 100, "max_memory_gb": 4, "max_hdd_gb": 100, "max_ssd_gb": 100},
    )
    with pytest.raises(QuotaExceededError, match="memory"):
        await uc.execute(MagicMock(), 60, uuid4(), uuid4())
