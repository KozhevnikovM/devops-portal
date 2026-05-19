from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.infrastructure.auth import require_admin
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.presentation.templating import templates

router = APIRouter()

_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()


# ── Catalog page ──────────────────────────────────────────────────────────────

@router.get("/admin/catalog", response_class=HTMLResponse)
async def admin_catalog_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    images = await _image_repo.list_all(session)
    hw_configs = await _hw_config_repo.list_all(session)
    return templates.TemplateResponse(
        request, "admin/catalog.html",
        {"images": images, "hw_configs": hw_configs, "current_user": current_user},
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
        error_html = f'<span class="text-red-400 text-xs">Image "{name}" already exists.</span>'
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
    memory_mb: int = Form(...),
    hdd_mb: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.create(session, name, cpus, memory_mb, hdd_mb)
    except IntegrityError:
        await session.rollback()
        error_html = f'<span class="text-red-400 text-xs">Config "{name}" already exists.</span>'
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
    memory_mb: int = Form(...),
    hdd_mb: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    try:
        await _hw_config_repo.update(
            session, hw_config_id,
            {"name": name, "cpus": cpus, "memory_mb": memory_mb, "hdd_mb": hdd_mb},
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
