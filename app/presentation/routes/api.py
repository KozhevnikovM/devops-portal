from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.domain.enums import DriveType
from app.infrastructure.auth import require_admin, require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

router = APIRouter(prefix="/api", tags=["admin"])

_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_static_vm_repo = StaticVMRepository()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class VMImageCreate(BaseModel):
    name: str
    vapp_template_id: str


class VMImageUpdate(BaseModel):
    name: Optional[str] = None
    vapp_template_id: Optional[str] = None
    is_active: Optional[bool] = None


class VMImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    vapp_template_id: str
    is_active: bool
    created_at: datetime


class HWConfigCreate(BaseModel):
    name: str
    cpus: int
    memory_mb: int
    disk_mb: int
    drive_type: DriveType = DriveType.HDD


class HWConfigUpdate(BaseModel):
    name: Optional[str] = None
    cpus: Optional[int] = None
    memory_mb: Optional[int] = None
    disk_mb: Optional[int] = None
    drive_type: Optional[DriveType] = None
    is_active: Optional[bool] = None


class HWConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    cpus: int
    memory_mb: int
    disk_mb: int
    drive_type: DriveType
    is_active: bool
    created_at: datetime


class StaticVMSummaryResponse(BaseModel):
    """Non-secret static-VM view for catalog discovery — never vends password/ssh_key."""
    id: UUID
    name: str
    host: str
    cpus: int | None
    memory_mb: int | None
    is_active: bool
    available: bool


# ── VM Images ─────────────────────────────────────────────────────────────────

@router.get("/images", response_model=list[VMImageResponse])
async def list_images(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),  # read-only catalog discovery for anyone ordering a VM
):
    return await _image_repo.list_all(session)


@router.post("/images", response_model=VMImageResponse, status_code=201)
async def create_image(
    body: VMImageCreate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    return await _image_repo.create(session, body.name, body.vapp_template_id)


@router.patch("/images/{image_id}", response_model=VMImageResponse)
async def update_image(
    image_id: UUID,
    body: VMImageUpdate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        return await _image_repo.update(session, image_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/images/{image_id}", status_code=204)
async def deactivate_image(
    image_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    try:
        await _image_repo.deactivate(session, image_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Hardware Configs ──────────────────────────────────────────────────────────

@router.get("/hardware", response_model=list[HWConfigResponse])
async def list_hardware(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),  # read-only catalog discovery for anyone ordering a VM
):
    return await _hw_config_repo.list_all(session)


@router.post("/hardware", response_model=HWConfigResponse, status_code=201)
async def create_hardware(
    body: HWConfigCreate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    return await _hw_config_repo.create(
        session, body.name, body.cpus, body.memory_mb, body.disk_mb,
        drive_type=body.drive_type.value,
    )


@router.patch("/hardware/{hw_config_id}", response_model=HWConfigResponse)
async def update_hardware(
    hw_config_id: UUID,
    body: HWConfigUpdate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        return await _hw_config_repo.update(session, hw_config_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/hardware/{hw_config_id}", status_code=204)
async def deactivate_hardware(
    hw_config_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.deactivate(session, hw_config_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Static VMs (discovery) ────────────────────────────────────────────────────

@router.get("/static-vms", response_model=list[StaticVMSummaryResponse])
async def list_static_vms(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),  # read-only discovery so names are orderable by anyone
):
    """List active static VMs (names discoverable for ordering). Credentials are never returned."""
    vms = await _static_vm_repo.list_active(session)
    held = await _static_vm_repo.held_by(session)
    return [
        StaticVMSummaryResponse(
            id=vm.id, name=vm.name, host=vm.host, cpus=vm.cpus, memory_mb=vm.memory_mb,
            is_active=vm.is_active, available=vm.id not in held,
        )
        for vm in vms
    ]
