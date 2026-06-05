from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.infrastructure.auth import require_admin
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository
from app.presentation.templating import templates

router = APIRouter()

_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_namespace_repo = NamespaceRepository()
_static_vm_repo = StaticVMRepository()


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
    return templates.TemplateResponse(
        request, "admin/catalog.html",
        {
            "images": images,
            "hw_configs": hw_configs,
            "namespaces": namespaces,
            "namespace_held_by": held_by,
            "static_vms": static_vms,
            "static_vm_held_by": static_vm_held_by,
            "current_user": current_user,
        },
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await _static_vm_table(request, session, current_user)
