import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from app.domain.entities import VMTemplate
from app.infrastructure.repositories.template_repo import TemplateRepository, _to_entity
from app.infrastructure.database.models import VMTemplateModel


def _make_model(**kwargs):
    m = MagicMock(spec=VMTemplateModel)
    m.id = kwargs.get("id", uuid4())
    m.name = kwargs.get("name", "Ubuntu 22.04")
    m.vapp_template_id = kwargs.get("vapp_template_id", "tpl-001")
    m.cpus = kwargs.get("cpus", 2)
    m.memory_mb = kwargs.get("memory_mb", 4096)
    m.disk_mb = kwargs.get("disk_mb", 26624)
    m.is_active = kwargs.get("is_active", True)
    m.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
    return m


def test_to_entity_maps_all_fields():
    model = _make_model()
    entity = _to_entity(model)
    assert entity.id == model.id
    assert entity.name == model.name
    assert entity.vapp_template_id == model.vapp_template_id
    assert entity.cpus == model.cpus
    assert entity.memory_mb == model.memory_mb
    assert entity.disk_mb == model.disk_mb
    assert entity.is_active == model.is_active


@pytest.mark.asyncio
async def test_list_active_returns_active_templates():
    repo = TemplateRepository()
    models = [_make_model(name="Ubuntu 22.04"), _make_model(name="Windows 2022")]

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = models
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await repo.list_active(mock_session)

    assert len(result) == 2
    assert all(isinstance(t, VMTemplate) for t in result)


@pytest.mark.asyncio
async def test_get_raises_for_missing_template():
    repo = TemplateRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_get_raises_for_inactive_template():
    repo = TemplateRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=_make_model(is_active=False))

    with pytest.raises(ValueError, match="inactive"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_get_returns_active_template():
    repo = TemplateRepository()
    model = _make_model()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=model)

    result = await repo.get(mock_session, model.id)

    assert isinstance(result, VMTemplate)
    assert result.id == model.id


def test_sync_get_raises_for_missing_template():
    repo = TemplateRepository()
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with pytest.raises(ValueError, match="not found"):
        repo.sync_get(mock_session, uuid4())


def test_sync_get_returns_template():
    repo = TemplateRepository()
    model = _make_model()
    mock_session = MagicMock()
    mock_session.get.return_value = model

    result = repo.sync_get(mock_session, model.id)

    assert isinstance(result, VMTemplate)
    assert result.vapp_template_id == model.vapp_template_id
