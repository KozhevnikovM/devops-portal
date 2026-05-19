import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from datetime import datetime, timezone

from app.domain.entities import VMImage, HWConfig
from app.infrastructure.repositories.image_repo import ImageRepository, _to_entity as _image_to_entity
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository, _to_entity as _hw_to_entity
from app.infrastructure.database.models import VMImageModel, HWConfigModel


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_image_model(**kwargs):
    m = MagicMock(spec=VMImageModel)
    m.id = kwargs.get("id", uuid4())
    m.name = kwargs.get("name", "Ubuntu 22.04")
    m.vapp_template_id = kwargs.get("vapp_template_id", "tpl-001")
    m.is_active = kwargs.get("is_active", True)
    m.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
    return m


def _make_hw_model(**kwargs):
    m = MagicMock(spec=HWConfigModel)
    m.id = kwargs.get("id", uuid4())
    m.name = kwargs.get("name", "medium")
    m.cpus = kwargs.get("cpus", 2)
    m.memory_mb = kwargs.get("memory_mb", 4096)
    m.hdd_mb = kwargs.get("hdd_mb", 26624)
    m.is_active = kwargs.get("is_active", True)
    m.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
    return m


# ── VMImage repo ──────────────────────────────────────────────────────────────

def test_image_to_entity_maps_all_fields():
    model = _make_image_model()
    entity = _image_to_entity(model)
    assert entity.id == model.id
    assert entity.name == model.name
    assert entity.vapp_template_id == model.vapp_template_id
    assert entity.is_active == model.is_active


@pytest.mark.asyncio
async def test_image_list_active_returns_only_active():
    repo = ImageRepository()
    models = [_make_image_model(name="Ubuntu 22.04"), _make_image_model(name="Windows 2022")]

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = models
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await repo.list_active(mock_session)
    assert len(result) == 2
    assert all(isinstance(t, VMImage) for t in result)


@pytest.mark.asyncio
async def test_image_get_raises_for_missing():
    repo = ImageRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_image_get_raises_for_inactive():
    repo = ImageRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=_make_image_model(is_active=False))

    with pytest.raises(ValueError, match="inactive"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_image_get_returns_active():
    repo = ImageRepository()
    model = _make_image_model()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=model)

    result = await repo.get(mock_session, model.id)
    assert isinstance(result, VMImage)
    assert result.id == model.id


def test_image_sync_get_raises_for_missing():
    repo = ImageRepository()
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with pytest.raises(ValueError, match="not found"):
        repo.sync_get(mock_session, uuid4())


def test_image_sync_get_returns_entity():
    repo = ImageRepository()
    model = _make_image_model()
    mock_session = MagicMock()
    mock_session.get.return_value = model

    result = repo.sync_get(mock_session, model.id)
    assert isinstance(result, VMImage)
    assert result.vapp_template_id == model.vapp_template_id


# ── HWConfig repo ─────────────────────────────────────────────────────────────

def test_hw_to_entity_maps_all_fields():
    model = _make_hw_model()
    entity = _hw_to_entity(model)
    assert entity.id == model.id
    assert entity.name == model.name
    assert entity.cpus == model.cpus
    assert entity.memory_mb == model.memory_mb
    assert entity.hdd_mb == model.hdd_mb
    assert entity.is_active == model.is_active


@pytest.mark.asyncio
async def test_hw_get_raises_for_missing():
    repo = HWConfigRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_hw_get_raises_for_inactive():
    repo = HWConfigRepository()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=_make_hw_model(is_active=False))

    with pytest.raises(ValueError, match="inactive"):
        await repo.get(mock_session, uuid4())


@pytest.mark.asyncio
async def test_hw_get_returns_active():
    repo = HWConfigRepository()
    model = _make_hw_model()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=model)

    result = await repo.get(mock_session, model.id)
    assert isinstance(result, HWConfig)
    assert result.cpus == model.cpus


def test_hw_sync_get_raises_for_missing():
    repo = HWConfigRepository()
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with pytest.raises(ValueError, match="not found"):
        repo.sync_get(mock_session, uuid4())


def test_hw_sync_get_returns_entity():
    repo = HWConfigRepository()
    model = _make_hw_model()
    mock_session = MagicMock()
    mock_session.get.return_value = model

    result = repo.sync_get(mock_session, model.id)
    assert isinstance(result, HWConfig)
    assert result.hdd_mb == model.hdd_mb
