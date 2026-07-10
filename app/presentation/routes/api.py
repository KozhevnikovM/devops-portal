from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import User
from app.domain.enums import DriveType
from app.infrastructure.auth import require_admin, require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.environment_blueprint_repo import EnvironmentBlueprintRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.role_repo import RoleRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

router = APIRouter(prefix="/api", tags=["admin"])

_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_namespace_repo = NamespaceRepository()
_role_repo = RoleRepository()
_blueprint_repo = EnvironmentBlueprintRepository()
_static_vm_repo = StaticVMRepository()

_VALID_RESOURCE_TYPES = {"VM", "STATIC_VM", "NAMESPACE"}


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


class NamespaceResponse(BaseModel):
    id: UUID
    name: str
    cluster_name: str
    api_url: str | None
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


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    ansible_role: str
    default_vars: dict = {}
    secret_vars: dict = {}


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    ansible_role: Optional[str] = None
    default_vars: Optional[dict] = None
    secret_vars: Optional[dict] = None  # None = keep existing; {} = clear
    is_active: Optional[bool] = None


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str]
    ansible_role: str
    default_vars: dict
    is_active: bool
    created_at: datetime


class BlueprintItemIn(BaseModel):
    resource_type: str
    label: Optional[str] = None
    spec: dict = {}


class BlueprintItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    resource_type: str
    position: int
    label: Optional[str]
    spec: dict


class BlueprintCreate(BaseModel):
    name: str
    description: Optional[str] = None
    items: list[BlueprintItemIn] = []


class BlueprintUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    items: Optional[list[BlueprintItemIn]] = None


class BlueprintResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    items: list[BlueprintItemResponse]


def _validate_blueprint_items(items: list[BlueprintItemIn]) -> list[dict]:
    """Validate resource_type + spec shape; return repo-ready item dicts (with position)."""
    out = []
    for idx, item in enumerate(items):
        if item.resource_type not in _VALID_RESOURCE_TYPES:
            raise HTTPException(status_code=400, detail=f"invalid resource_type '{item.resource_type}'")
        if item.resource_type == "VM" and not (item.spec.get("image_name") and item.spec.get("hw_config_name")):
            raise HTTPException(
                status_code=400,
                detail="a VM item needs image_name and hw_config_name in its spec",
            )
        out.append({
            "resource_type": item.resource_type, "label": item.label,
            "position": idx, "spec": item.spec,
        })
    return out


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


# ── Namespaces (discovery) ────────────────────────────────────────────────────

_VALID_NS_FILTERS = {"active", "available"}


@router.get("/namespaces", response_model=list[NamespaceResponse])
async def list_namespaces(
    filter: str = "active",
    username: str | None = None,
    not_username: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),
):
    """List namespaces from the catalog.

    - `filter=active` (default) — all active namespaces
    - `filter=available` — active namespaces not currently held by any booking
    - `username=X` — active namespaces currently held by user X
    - `not_username=X` — active namespaces NOT held by user X
    """
    if filter not in _VALID_NS_FILTERS:
        raise HTTPException(status_code=400, detail=f"invalid filter '{filter}'; use 'active' or 'available'")
    if username and not_username:
        raise HTTPException(status_code=400, detail="username and not_username are mutually exclusive")
    if username:
        return await _namespace_repo.list_held_by_username(session, username)
    if not_username:
        return await _namespace_repo.list_active_not_held_by_username(session, not_username)
    if filter == "available":
        return await _namespace_repo.list_available(session)
    return await _namespace_repo.list_active(session)


# ── Ansible Roles ─────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),  # read-only discovery so role names are orderable by anyone
):
    return await _role_repo.list_all(session)


@router.post("/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    body: RoleCreate,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    secret_vars = body.secret_vars if settings.SECRET_VARS_ENABLED else {}
    try:
        return await _role_repo.create(
            session, body.name, body.description, body.ansible_role, body.default_vars,
            secret_vars=secret_vars, actor=current_user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"Role '{body.name}' already exists")


@router.patch("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: UUID,
    body: RoleUpdate,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    fields = body.model_dump(exclude_none=True)
    if not settings.SECRET_VARS_ENABLED:
        fields.pop("secret_vars", None)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        return await _role_repo.update(session, role_id, fields, actor=current_user.username)
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/roles/{role_id}", status_code=204)
async def deactivate_role(
    role_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    try:
        await _role_repo.deactivate(session, role_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Environment Blueprints ────────────────────────────────────────────────────

@router.get("/environment-blueprints", response_model=list[BlueprintResponse])
async def list_blueprints(
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_user),  # read-only discovery so users can see what they can order
):
    return await _blueprint_repo.list_all(session)


@router.post("/environment-blueprints", response_model=BlueprintResponse, status_code=201)
async def create_blueprint(
    body: BlueprintCreate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    items = _validate_blueprint_items(body.items)
    try:
        return await _blueprint_repo.create(session, body.name, body.description, items)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"Blueprint '{body.name}' already exists")


@router.patch("/environment-blueprints/{blueprint_id}", response_model=BlueprintResponse)
async def update_blueprint(
    blueprint_id: UUID,
    body: BlueprintUpdate,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    fields = body.model_dump(exclude_none=True, exclude={"items"})
    items = _validate_blueprint_items(body.items) if body.items is not None else None
    if not fields and items is None:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        return await _blueprint_repo.update(session, blueprint_id, fields, items)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Blueprint name already exists")


@router.delete("/environment-blueprints/{blueprint_id}", status_code=204)
async def deactivate_blueprint(
    blueprint_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    try:
        await _blueprint_repo.deactivate(session, blueprint_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
