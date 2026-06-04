"""Regression tests for #93: max_ssd_gb missing from quota code."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.domain.entities import Quota
from app.infrastructure.repositories.quota_repo import QuotaRepository, _to_entity
from app.infrastructure.database.models import QuotaModel


def _make_quota_model(**kwargs):
    m = MagicMock(spec=QuotaModel)
    m.id = kwargs.get("id", uuid4())
    m.user_id = kwargs.get("user_id", uuid4())
    m.max_cpus = kwargs.get("max_cpus", 16)
    m.max_memory_gb = kwargs.get("max_memory_gb", 32)
    m.max_ssd_gb = kwargs.get("max_ssd_gb", 500)
    m.max_hdd_gb = kwargs.get("max_hdd_gb", 500)
    m.created_at = kwargs.get("created_at", None)
    return m


def test_to_entity_includes_max_ssd_gb():
    model = _make_quota_model(max_ssd_gb=200)
    entity = _to_entity(model)
    assert isinstance(entity, Quota)
    assert entity.max_ssd_gb == 200


@pytest.mark.asyncio
async def test_get_limits_includes_max_ssd_gb():
    repo = QuotaRepository()
    model = _make_quota_model(max_ssd_gb=250)
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    mock_session.execute = AsyncMock(return_value=mock_result)

    limits = await repo.get_limits(mock_session, str(model.user_id))
    assert "max_ssd_gb" in limits
    assert limits["max_ssd_gb"] == 250


@pytest.mark.asyncio
async def test_get_limits_defaults_when_no_row():
    from app.config import settings
    repo = QuotaRepository()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    limits = await repo.get_limits(mock_session, str(uuid4()))
    assert limits["max_ssd_gb"] == settings.DEFAULT_QUOTA_SSD_GB


@pytest.mark.asyncio
async def test_get_limits_for_update_includes_max_ssd_gb():
    # #142: get_limits_for_update lazy-seeds the row (ON CONFLICT DO NOTHING) and then
    # SELECT ... FOR UPDATE, so the row always exists and we read it with scalar_one().
    repo = QuotaRepository()
    model = _make_quota_model(max_ssd_gb=300)
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = model
    mock_session.execute = AsyncMock(return_value=mock_result)

    limits = await repo.get_limits_for_update(mock_session, str(model.user_id))
    assert limits["max_ssd_gb"] == 300
    # Two statements: the lazy-seed insert and the locking select.
    assert mock_session.execute.await_count == 2


@pytest.mark.asyncio
async def test_set_includes_max_ssd_gb_in_upsert():
    repo = QuotaRepository()
    user_id = uuid4()
    saved_model = _make_quota_model(user_id=user_id, max_cpus=8, max_memory_gb=16,
                                    max_ssd_gb=400, max_hdd_gb=200)
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = saved_model
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    quota = await repo.set(mock_session, user_id=user_id,
                           max_cpus=8, max_memory_gb=16, max_ssd_gb=400, max_hdd_gb=200)
    assert quota.max_ssd_gb == 400


def test_count_active_resources_returns_ssd_gb_zero():
    """count_active_resources returns ssd_gb=0 until bookings track SSD."""
    import asyncio
    repo = QuotaRepository()
    mock_row = MagicMock()
    mock_row.cpus = 4
    mock_row.memory_mb = 4096
    mock_row.hdd_mb = 26624
    mock_result = MagicMock()
    mock_result.one.return_value = mock_row
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = asyncio.run(repo.count_active_resources(mock_session, str(uuid4())))
    assert result["ssd_gb"] == 0
