"""Regression tests for #142 — quota lock is effective for default-quota users.

Before the fix, a default-quota user had no QuotaModel row, so SELECT ... FOR UPDATE locked
nothing; and CreateBookingUseCase counted usage before taking the lock. Two concurrent bookings
could both pass. After the fix get_limits_for_update lazy-seeds the row then locks it, and the
use case takes the lock before counting.
"""
from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

import pytest

from app.infrastructure.repositories.quota_repo import QuotaRepository
from app.infrastructure.database.models import QuotaModel


def _quota_model():
    m = MagicMock(spec=QuotaModel)
    m.id = uuid4()
    m.user_id = uuid4()
    m.max_cpus = 16
    m.max_memory_gb = 32
    m.max_ssd_gb = 500
    m.max_hdd_gb = 500
    m.created_at = None
    return m


@pytest.mark.asyncio
async def test_get_limits_for_update_seeds_then_locks():
    """Even with no pre-existing row, an INSERT and a locking SELECT are issued."""
    repo = QuotaRepository()
    model = _quota_model()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = model
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    limits = await repo.get_limits_for_update(mock_session, str(model.user_id))

    assert limits["max_cpus"] == 16
    # Two statements: lazy-seed insert (ON CONFLICT DO NOTHING) + FOR UPDATE select.
    assert mock_session.execute.await_count == 2
    insert_sql = str(mock_session.execute.await_args_list[0].args[0]).lower()
    select_sql = str(mock_session.execute.await_args_list[1].args[0]).lower()
    assert "insert into" in insert_sql and "on conflict" in insert_sql
    assert "for update" in select_sql


@pytest.mark.asyncio
async def test_create_booking_locks_before_counting():
    """The lock (get_limits_for_update) must be taken before counting usage."""
    from datetime import datetime, timezone
    from app.application.use_cases.create_booking import CreateBookingUseCase
    from app.domain.entities import VMImage, HWConfig

    now = datetime.now(timezone.utc)
    image = VMImage(id=uuid4(), name="Ubuntu", vapp_template_id="tpl", is_active=True, created_at=now)
    hw = HWConfig(id=uuid4(), name="medium", cpus=2, memory_mb=4096, hdd_mb=26624, is_active=True, created_at=now)

    image_repo = MagicMock()
    image_repo.get = AsyncMock(return_value=image)
    hw_repo = MagicMock()
    hw_repo.get = AsyncMock(return_value=hw)

    quota_repo = MagicMock()
    order = MagicMock()  # records the call ordering across both quota methods

    async def _lock(*a, **k):
        order.lock()
        return {"max_cpus": 100, "max_memory_gb": 1000, "max_hdd_gb": 1000}

    async def _count(*a, **k):
        order.count()
        return {"cpus": 0, "memory_gb": 0, "hdd_gb": 0}

    quota_repo.get_limits_for_update = AsyncMock(side_effect=_lock)
    quota_repo.count_active_resources = AsyncMock(side_effect=_count)

    booking_repo = MagicMock()
    booking_repo.create = AsyncMock(side_effect=lambda s, b: b)

    use_case = CreateBookingUseCase(booking_repo, image_repo, hw_repo, quota_repo)
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("app.application.use_cases.create_booking.provision_vm_task", MagicMock())
        await use_case.execute(AsyncMock(), ttl_minutes=60, image_id=image.id, hw_config_id=hw.id, user_id=str(uuid4()))

    assert order.mock_calls == [call.lock(), call.count()]
