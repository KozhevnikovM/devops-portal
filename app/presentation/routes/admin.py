import json
from uuid import UUID

import yaml

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError, NotFoundError
from app.infrastructure.auth import require_admin
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.environment_blueprint_repo import EnvironmentBlueprintRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.role_repo import RoleRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository
from app.presentation.deps import dispatcher
from app.presentation.templating import templates

router = APIRouter()

_booking_repo = BookingRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_namespace_repo = NamespaceRepository()
_role_repo = RoleRepository()
_blueprint_repo = EnvironmentBlueprintRepository()
_static_vm_repo = StaticVMRepository()

_VALID_RESOURCE_TYPES = {"VM", "STATIC_VM", "NAMESPACE"}


def _parse_default_vars(raw: str) -> dict:
    """Parse the default_vars YAML textarea; raise ValueError on bad YAML or a non-mapping."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"default_vars must be valid YAML: {exc}")
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("default_vars must be a YAML mapping (key: value pairs)")
    return parsed


def _parse_secret_vars(raw: str) -> dict | None:
    """Parse the secret_vars JSON textarea.

    Returns None when blank (meaning "keep existing").
    Returns {} when the field is explicitly ``{}``.
    Raises ValueError on bad JSON or non-object.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"secret_vars must be valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ValueError("secret_vars must be a JSON object")
    return parsed


def _parse_blueprint_items(raw: str) -> list[dict]:
    """Parse the blueprint items JSON-array textarea; raise ValueError on bad shape."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"items must be valid JSON: {exc}")
    if not isinstance(parsed, list):
        raise ValueError("items must be a JSON array")
    out = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError("each item must be a JSON object")
        rt = item.get("resource_type")
        if rt not in _VALID_RESOURCE_TYPES:
            raise ValueError(f"item {idx}: invalid resource_type '{rt}'")
        spec = item.get("spec") or {}
        if not isinstance(spec, dict):
            raise ValueError(f"item {idx}: spec must be a JSON object")
        if rt == "VM" and not (spec.get("image_name") and spec.get("hw_config_name")):
            raise ValueError(f"item {idx}: a VM item needs image_name and hw_config_name in spec")
        out.append({"resource_type": rt, "label": item.get("label"), "position": idx, "spec": spec})
    return out


# ── Catalog page ──────────────────────────────────────────────────────────────

@router.get("/admin/catalog", response_class=HTMLResponse)
async def admin_catalog_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    images = await _image_repo.list_all(session)
    hw_configs = await _hw_config_repo.list_all(session)
    namespaces = await _namespace_repo.list_all(session)
    held_by = await _namespace_repo.held_by(session)
    static_vms = await _static_vm_repo.list_all(session)
    static_vm_held_by = await _static_vm_repo.held_by(session)
    roles = await _role_repo.list_all(session)
    blueprints = await _blueprint_repo.list_all(session)
    return templates.TemplateResponse(
        request, "admin/catalog.html",
        {
            "images": images,
            "hw_configs": hw_configs,
            "namespaces": namespaces,
            "namespace_held_by": held_by,
            "static_vms": static_vms,
            "static_vm_held_by": static_vm_held_by,
            "roles": roles,
            "blueprints": blueprints,
            "current_user": current_user,
            "settings": settings,
        },
    )


# ── Admin booking actions ─────────────────────────────────────────────────────

@router.post("/admin/bookings/{booking_id}/force-release", response_class=HTMLResponse)
async def admin_force_release_booking(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        booking = await _booking_repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.resource_type != ResourceType.VM:
        raise HTTPException(status_code=400, detail="Force release is only available for VM bookings")
    if booking.status != BookingStatus.FAILED:
        raise HTTPException(status_code=400, detail=f"Booking is {booking.status.value}, not FAILED")

    await _booking_repo.update_status(session, booking_id, BookingStatus.RELEASING, actor_id=str(current_user.id))
    dispatcher.dispatch_teardown_force(str(booking_id))

    booking = await _booking_repo.get(session, booking_id)
    return templates.TemplateResponse(
        request, "partials/booking_row.html",
        {"booking": booking, "current_user": current_user},
        status_code=202,
    )


# ── VM Images ─────────────────────────────────────────────────────────────────

@router.get("/admin/catalog/images/table", response_class=HTMLResponse)
async def image_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


@router.post("/admin/catalog/images", response_class=HTMLResponse)
async def admin_create_image(
    request: Request,
    name: str = Form(...),
    vapp_template_id: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _image_repo.create(session, name, vapp_template_id)
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Image "{escape(name)}" already exists.</span>'
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#image-create-error", "HX-Reswap": "innerHTML"},
        )
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


@router.get("/admin/catalog/images/{image_id}/edit", response_class=HTMLResponse)
async def admin_edit_image_form(
    request: Request,
    image_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    images = await _image_repo.list_all(session)
    editing_image = next((img for img in images if img.id == image_id), None)
    if editing_image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "editing_image": editing_image, "current_user": current_user},
    )


@router.patch("/admin/catalog/images/{image_id}", response_class=HTMLResponse)
async def admin_update_image(
    request: Request,
    image_id: UUID,
    name: str = Form(...),
    vapp_template_id: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _image_repo.update(session, image_id, {"name": name, "vapp_template_id": vapp_template_id})
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


@router.post("/admin/catalog/images/{image_id}/activate", response_class=HTMLResponse)
async def admin_activate_image(
    request: Request,
    image_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _image_repo.activate(session, image_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


@router.delete("/admin/catalog/images/{image_id}/permanent", response_class=HTMLResponse)
async def admin_delete_image(
    request: Request,
    image_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _image_repo.delete(session, image_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        return HTMLResponse(
            content='<span class="text-red-400 text-xs">Cannot delete: bookings reference this image.</span>',
            headers={"HX-Retarget": f"#image-delete-error-{image_id}", "HX-Reswap": "innerHTML"},
        )
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


@router.delete("/admin/catalog/images/{image_id}", response_class=HTMLResponse)
async def admin_deactivate_image(
    request: Request,
    image_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _image_repo.deactivate(session, image_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    images = await _image_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/image_table.html",
        {"images": images, "current_user": current_user},
    )


# ── Hardware Configs ──────────────────────────────────────────────────────────

@router.get("/admin/catalog/hardware/table", response_class=HTMLResponse)
async def hw_config_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


@router.post("/admin/catalog/hardware", response_class=HTMLResponse)
async def admin_create_hw_config(
    request: Request,
    name: str = Form(...),
    cpus: int = Form(...),
    memory_gb: int = Form(...),
    disk_gb: int = Form(...),
    drive_type: str = Form("HDD"),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.create(
            session, name, cpus, memory_gb * 1024, disk_gb * 1024, drive_type=drive_type
        )
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Config "{escape(name)}" already exists.</span>'
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#hw-create-error", "HX-Reswap": "innerHTML"},
        )
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


@router.get("/admin/catalog/hardware/{hw_config_id}/edit", response_class=HTMLResponse)
async def admin_edit_hw_config_form(
    request: Request,
    hw_config_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    hw_configs = await _hw_config_repo.list_all(session)
    editing_hw = next((hw for hw in hw_configs if hw.id == hw_config_id), None)
    if editing_hw is None:
        raise HTTPException(status_code=404, detail="Hardware config not found")
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "editing_hw": editing_hw, "current_user": current_user},
    )


@router.patch("/admin/catalog/hardware/{hw_config_id}", response_class=HTMLResponse)
async def admin_update_hw_config(
    request: Request,
    hw_config_id: UUID,
    name: str = Form(...),
    cpus: int = Form(...),
    memory_gb: int = Form(...),
    disk_gb: int = Form(...),
    drive_type: str = Form("HDD"),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.update(
            session, hw_config_id,
            {"name": name, "cpus": cpus, "memory_mb": memory_gb * 1024,
             "disk_mb": disk_gb * 1024, "drive_type": drive_type},
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


@router.post("/admin/catalog/hardware/{hw_config_id}/activate", response_class=HTMLResponse)
async def admin_activate_hw_config(
    request: Request,
    hw_config_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.activate(session, hw_config_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


@router.delete("/admin/catalog/hardware/{hw_config_id}/permanent", response_class=HTMLResponse)
async def admin_delete_hw_config(
    request: Request,
    hw_config_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.delete(session, hw_config_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        return HTMLResponse(
            content='<span class="text-red-400 text-xs">Cannot delete: bookings reference this config.</span>',
            headers={"HX-Retarget": f"#hw-delete-error-{hw_config_id}", "HX-Reswap": "innerHTML"},
        )
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


@router.delete("/admin/catalog/hardware/{hw_config_id}", response_class=HTMLResponse)
async def admin_deactivate_hw_config(
    request: Request,
    hw_config_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.deactivate(session, hw_config_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/hw_config_table.html",
        {"hw_configs": hw_configs, "current_user": current_user},
    )


# ── Namespaces ──────────────────────────────────────────────────────────────

async def _namespace_table(request, session, current_user):
    namespaces = await _namespace_repo.list_all(session)
    held_by = await _namespace_repo.held_by(session)
    return templates.TemplateResponse(
        request, "partials/namespace_table.html",
        {"namespaces": namespaces, "namespace_held_by": held_by, "current_user": current_user},
    )


@router.get("/admin/catalog/namespaces/table", response_class=HTMLResponse)
async def namespace_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    return await _namespace_table(request, session, current_user)


@router.post("/admin/catalog/namespaces", response_class=HTMLResponse)
async def admin_create_namespace(
    request: Request,
    name: str = Form(...),
    cluster_name: str = Form(...),
    api_url: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _namespace_repo.create(session, name, cluster_name, api_url.strip() or None)
    except IntegrityError:
        await session.rollback()
        error_html = (
            f'<span class="text-red-400 text-xs">Namespace "{escape(name)}" already exists '
            f'on cluster "{escape(cluster_name)}".</span>'
        )
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#namespace-create-error", "HX-Reswap": "innerHTML"},
        )
    return await _namespace_table(request, session, current_user)


@router.get("/admin/catalog/namespaces/{namespace_id}/edit", response_class=HTMLResponse)
async def admin_edit_namespace_form(
    request: Request,
    namespace_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    namespaces = await _namespace_repo.list_all(session)
    editing_namespace = next((ns for ns in namespaces if ns.id == namespace_id), None)
    if editing_namespace is None:
        raise HTTPException(status_code=404, detail="Namespace not found")
    held_by = await _namespace_repo.held_by(session)
    return templates.TemplateResponse(
        request, "partials/namespace_table.html",
        {
            "namespaces": namespaces,
            "namespace_held_by": held_by,
            "editing_namespace": editing_namespace,
            "current_user": current_user,
        },
    )


@router.patch("/admin/catalog/namespaces/{namespace_id}", response_class=HTMLResponse)
async def admin_update_namespace(
    request: Request,
    namespace_id: UUID,
    name: str = Form(...),
    cluster_name: str = Form(...),
    api_url: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _namespace_repo.update(
            session, namespace_id,
            {"name": name, "cluster_name": cluster_name, "api_url": api_url.strip() or None},
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        error_html = (
            f'<span class="text-red-400 text-xs">Namespace "{escape(name)}" already exists '
            f'on cluster "{escape(cluster_name)}".</span>'
        )
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#namespace-create-error", "HX-Reswap": "innerHTML"},
        )
    return await _namespace_table(request, session, current_user)


@router.post("/admin/catalog/namespaces/{namespace_id}/activate", response_class=HTMLResponse)
async def admin_activate_namespace(
    request: Request,
    namespace_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _namespace_repo.activate(session, namespace_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _namespace_table(request, session, current_user)


@router.delete("/admin/catalog/namespaces/{namespace_id}/permanent", response_class=HTMLResponse)
async def admin_delete_namespace(
    request: Request,
    namespace_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _namespace_repo.delete(session, namespace_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        return HTMLResponse(
            content='<span class="text-red-400 text-xs">Cannot delete: bookings reference this namespace.</span>',
            headers={"HX-Retarget": f"#namespace-delete-error-{namespace_id}", "HX-Reswap": "innerHTML"},
        )
    return await _namespace_table(request, session, current_user)


@router.delete("/admin/catalog/namespaces/{namespace_id}", response_class=HTMLResponse)
async def admin_deactivate_namespace(
    request: Request,
    namespace_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _namespace_repo.deactivate(session, namespace_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _namespace_table(request, session, current_user)


# ── Static VMs ────────────────────────────────────────────────────────────────

async def _static_vm_table(request, session, current_user):
    static_vms = await _static_vm_repo.list_all(session)
    held_by = await _static_vm_repo.held_by(session)
    return templates.TemplateResponse(
        request, "partials/static_vm_table.html",
        {"static_vms": static_vms, "static_vm_held_by": held_by, "current_user": current_user},
    )


def _gb_to_mb(value: str) -> int | None:
    value = value.strip()
    return int(value) * 1024 if value else None


def _credential_error() -> HTMLResponse:
    return HTMLResponse(
        content='<span class="text-red-400 text-xs">Provide a password or an SSH key.</span>',
        headers={"HX-Retarget": "#static-vm-create-error", "HX-Reswap": "innerHTML"},
    )


@router.get("/admin/catalog/static-vms/table", response_class=HTMLResponse)
async def static_vm_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    return await _static_vm_table(request, session, current_user)


@router.post("/admin/catalog/static-vms", response_class=HTMLResponse)
async def admin_create_static_vm(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    ssh_key: str = Form(""),
    cpus: str = Form(""),
    memory_gb: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    if not password.strip() and not ssh_key.strip():
        return _credential_error()
    try:
        await _static_vm_repo.create(
            session, name, host, username,
            password.strip() or None,
            ssh_key.strip() or None,
            int(cpus.strip()) if cpus.strip() else None,
            _gb_to_mb(memory_gb),
        )
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Static VM "{escape(name)}" already exists.</span>'
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#static-vm-create-error", "HX-Reswap": "innerHTML"},
        )
    return await _static_vm_table(request, session, current_user)


@router.get("/admin/catalog/static-vms/{static_vm_id}/edit", response_class=HTMLResponse)
async def admin_edit_static_vm_form(
    request: Request,
    static_vm_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    static_vms = await _static_vm_repo.list_all(session)
    editing_static_vm = next((vm for vm in static_vms if vm.id == static_vm_id), None)
    if editing_static_vm is None:
        raise HTTPException(status_code=404, detail="Static VM not found")
    held_by = await _static_vm_repo.held_by(session)
    return templates.TemplateResponse(
        request, "partials/static_vm_table.html",
        {
            "static_vms": static_vms,
            "static_vm_held_by": held_by,
            "editing_static_vm": editing_static_vm,
            "current_user": current_user,
        },
    )


@router.patch("/admin/catalog/static-vms/{static_vm_id}", response_class=HTMLResponse)
async def admin_update_static_vm(
    request: Request,
    static_vm_id: UUID,
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    ssh_key: str = Form(""),
    cpus: str = Form(""),
    memory_gb: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    if not password.strip() and not ssh_key.strip():
        return _credential_error()
    try:
        await _static_vm_repo.update(
            session, static_vm_id,
            {
                "name": name,
                "host": host,
                "username": username,
                "password": password.strip() or None,
                "ssh_key": ssh_key.strip() or None,
                "cpus": int(cpus.strip()) if cpus.strip() else None,
                "memory_mb": _gb_to_mb(memory_gb),
            },
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Static VM "{escape(name)}" already exists.</span>'
        return HTMLResponse(
            content=error_html,
            headers={"HX-Retarget": "#static-vm-create-error", "HX-Reswap": "innerHTML"},
        )
    return await _static_vm_table(request, session, current_user)


@router.post("/admin/catalog/static-vms/{static_vm_id}/activate", response_class=HTMLResponse)
async def admin_activate_static_vm(
    request: Request,
    static_vm_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _static_vm_repo.activate(session, static_vm_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _static_vm_table(request, session, current_user)


@router.delete("/admin/catalog/static-vms/{static_vm_id}/permanent", response_class=HTMLResponse)
async def admin_delete_static_vm(
    request: Request,
    static_vm_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _static_vm_repo.delete(session, static_vm_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        return HTMLResponse(
            content='<span class="text-red-400 text-xs">Cannot delete: bookings reference this static VM.</span>',
            headers={"HX-Retarget": f"#static-vm-delete-error-{static_vm_id}", "HX-Reswap": "innerHTML"},
        )
    return await _static_vm_table(request, session, current_user)


@router.delete("/admin/catalog/static-vms/{static_vm_id}", response_class=HTMLResponse)
async def admin_deactivate_static_vm(
    request: Request,
    static_vm_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _static_vm_repo.deactivate(session, static_vm_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _static_vm_table(request, session, current_user)


# ── Ansible Roles ─────────────────────────────────────────────────────────────

async def _role_table(request, session, current_user, editing_role=None, secret_vars_scaffold=None):
    roles = await _role_repo.list_all(session)
    return templates.TemplateResponse(
        request, "partials/role_table.html",
        {"roles": roles, "editing_role": editing_role, "current_user": current_user,
         "settings": settings, "secret_vars_scaffold": secret_vars_scaffold},
    )


def _role_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        content=f'<span class="text-red-400 text-xs">{escape(message)}</span>',
        headers={"HX-Retarget": "#role-create-error", "HX-Reswap": "innerHTML"},
    )


@router.get("/admin/catalog/roles/table", response_class=HTMLResponse)
async def role_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    return await _role_table(request, session, current_user)


@router.post("/admin/catalog/roles", response_class=HTMLResponse)
async def admin_create_role(
    request: Request,
    name: str = Form(...),
    ansible_role: str = Form(...),
    description: str = Form(""),
    default_vars: str = Form(""),
    secret_vars: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        parsed_vars = _parse_default_vars(default_vars)
        parsed_secrets = _parse_secret_vars(secret_vars) if settings.SECRET_VARS_ENABLED else None
    except ValueError as exc:
        return _role_error(str(exc))
    try:
        await _role_repo.create(
            session, name, description.strip() or None, ansible_role, parsed_vars,
            secret_vars=parsed_secrets or {}, actor=current_user.username,
        )
    except ValueError as exc:
        return _role_error(str(exc))
    except IntegrityError:
        await session.rollback()
        return _role_error(f'Role "{name}" already exists.')
    return await _role_table(request, session, current_user)


@router.get("/admin/catalog/roles/{role_id}/edit", response_class=HTMLResponse)
async def admin_edit_role_form(
    request: Request,
    role_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    roles = await _role_repo.list_all(session)
    editing_role = next((r for r in roles if r.id == role_id), None)
    if editing_role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    scaffold = {k: "" for k in editing_role.secret_vars} if editing_role.secret_vars else None
    return await _role_table(request, session, current_user,
                             editing_role=editing_role, secret_vars_scaffold=scaffold)


@router.patch("/admin/catalog/roles/{role_id}", response_class=HTMLResponse)
async def admin_update_role(
    request: Request,
    role_id: UUID,
    name: str = Form(...),
    ansible_role: str = Form(...),
    description: str = Form(""),
    default_vars: str = Form(""),
    secret_vars: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        parsed_vars = _parse_default_vars(default_vars)
        parsed_secrets = _parse_secret_vars(secret_vars) if settings.SECRET_VARS_ENABLED else None
    except ValueError as exc:
        return _role_error(str(exc))
    fields = {
        "name": name, "ansible_role": ansible_role,
        "description": description.strip() or None, "default_vars": parsed_vars,
    }
    if parsed_secrets is not None:
        fields["secret_vars"] = parsed_secrets
    try:
        await _role_repo.update(session, role_id, fields, actor=current_user.username)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        return _role_error(str(exc))
    except IntegrityError:
        await session.rollback()
        return _role_error(f'Role "{name}" already exists.')
    return await _role_table(request, session, current_user)


@router.post("/admin/catalog/roles/{role_id}/activate", response_class=HTMLResponse)
async def admin_activate_role(
    request: Request,
    role_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _role_repo.activate(session, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _role_table(request, session, current_user)


@router.delete("/admin/catalog/roles/{role_id}/permanent", response_class=HTMLResponse)
async def admin_delete_role(
    request: Request,
    role_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _role_repo.delete(session, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _role_table(request, session, current_user)


@router.delete("/admin/catalog/roles/{role_id}", response_class=HTMLResponse)
async def admin_deactivate_role(
    request: Request,
    role_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _role_repo.deactivate(session, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _role_table(request, session, current_user)


# ── Environment Blueprints ────────────────────────────────────────────────────

async def _blueprint_table(request, session, current_user, editing_blueprint=None):
    blueprints = await _blueprint_repo.list_all(session)
    editing_items_json = ""
    if editing_blueprint is not None:
        editing_items_json = json.dumps(
            [
                {"resource_type": it.resource_type, "label": it.label, "spec": it.spec}
                for it in editing_blueprint.items
            ],
            indent=2,
        )
    return templates.TemplateResponse(
        request, "partials/blueprint_table.html",
        {
            "blueprints": blueprints, "editing_blueprint": editing_blueprint,
            "editing_items_json": editing_items_json, "current_user": current_user,
        },
    )


def _blueprint_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        content=f'<span class="text-red-400 text-xs">{escape(message)}</span>',
        headers={"HX-Retarget": "#blueprint-create-error", "HX-Reswap": "innerHTML"},
    )


@router.get("/admin/catalog/blueprints/table", response_class=HTMLResponse)
async def blueprint_table(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    return await _blueprint_table(request, session, current_user)


@router.post("/admin/catalog/blueprints", response_class=HTMLResponse)
async def admin_create_blueprint(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    items: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        parsed_items = _parse_blueprint_items(items)
    except ValueError as exc:
        return _blueprint_error(str(exc))
    try:
        await _blueprint_repo.create(session, name, description.strip() or None, parsed_items)
    except IntegrityError:
        await session.rollback()
        return _blueprint_error(f'Blueprint "{name}" already exists.')
    return await _blueprint_table(request, session, current_user)


@router.get("/admin/catalog/blueprints/{blueprint_id}/edit", response_class=HTMLResponse)
async def admin_edit_blueprint_form(
    request: Request,
    blueprint_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        editing = await _blueprint_repo.get(session, blueprint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return await _blueprint_table(request, session, current_user, editing_blueprint=editing)


@router.patch("/admin/catalog/blueprints/{blueprint_id}", response_class=HTMLResponse)
async def admin_update_blueprint(
    request: Request,
    blueprint_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    items: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        parsed_items = _parse_blueprint_items(items)
    except ValueError as exc:
        return _blueprint_error(str(exc))
    try:
        await _blueprint_repo.update(
            session, blueprint_id,
            {"name": name, "description": description.strip() or None}, parsed_items,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IntegrityError:
        await session.rollback()
        return _blueprint_error(f'Blueprint "{name}" already exists.')
    return await _blueprint_table(request, session, current_user)


@router.post("/admin/catalog/blueprints/{blueprint_id}/activate", response_class=HTMLResponse)
async def admin_activate_blueprint(
    request: Request,
    blueprint_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _blueprint_repo.activate(session, blueprint_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _blueprint_table(request, session, current_user)


@router.delete("/admin/catalog/blueprints/{blueprint_id}/permanent", response_class=HTMLResponse)
async def admin_delete_blueprint(
    request: Request,
    blueprint_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _blueprint_repo.delete(session, blueprint_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _blueprint_table(request, session, current_user)


@router.delete("/admin/catalog/blueprints/{blueprint_id}", response_class=HTMLResponse)
async def admin_deactivate_blueprint(
    request: Request,
    blueprint_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _blueprint_repo.deactivate(session, blueprint_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _blueprint_table(request, session, current_user)
