from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases._permissions import can_manage
from app.domain.entities import User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError, BookingNotFoundError, NamespaceUnavailableError, BookingPermissionError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation import deps
from app.presentation.templating import templates

router = APIRouter()

# Shared singletons from the composition root. Names kept so existing patches still target them.
_repo = deps.booking_repo
_image_repo = deps.image_repo
_hw_config_repo = deps.hw_config_repo
_namespace_repo = deps.namespace_repo
_static_vm_repo = deps.static_vm_repo
_dispatcher = deps.dispatcher
_use_case = deps.create_booking_uc
_extend_use_case = deps.extend_booking_uc
_release_use_case = deps.release_booking_uc
_book_namespace_use_case = deps.book_namespace_uc
_reserve_static_vm_use_case = deps.reserve_static_vm_uc

# Resource types listed on each booking page.
_VM_PAGE_TYPES = [ResourceType.VM.value, ResourceType.STATIC_VM.value]


async def _attach_queue_position(session, booking) -> None:
    """Populate FIFO rank for a QUEUED booking (display only)."""
    if booking.status == BookingStatus.QUEUED:
        booking.queue_position = await _repo.queue_position(
            session, booking.resource_type.value, booking.created_at
        )


async def _render_bookings_page(
    request, session, current_user, *, booking_type, page_path, active_nav, filter, show_released,
):
    # The VM page lists both provisioned and static VMs; other pages list their one type.
    query_types = _VM_PAGE_TYPES if booking_type == "VM" else booking_type

    if filter == "all":
        bookings = await _repo.list_all(
            session, include_released=show_released, resource_type=query_types
        )
    else:
        bookings = await _repo.list_by_user(
            session, str(current_user.id), include_released=show_released, resource_type=query_types
        )
    for b in bookings:
        await _attach_queue_position(session, b)
    vm_images = await _image_repo.list_active(session)
    hw_configs = await _hw_config_repo.list_active(session)
    available_namespaces = await _namespace_repo.list_available(session)
    available_static_vms = await _static_vm_repo.list_available(session)
    return templates.TemplateResponse(
        request, "index.html",
        {
            "bookings": bookings,
            "vm_images": vm_images,
            "hw_configs": hw_configs,
            "available_namespaces": available_namespaces,
            "available_static_vms": available_static_vms,
            "current_user": current_user,
            "active_filter": filter,
            "show_released": show_released,
            "booking_type": booking_type,
            "page_path": page_path,
            "active_nav": active_nav,
        },
    )


@router.get("/", response_class=HTMLResponse)
@router.get("/book/vm", response_class=HTMLResponse)
async def vm_bookings_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_bookings_page(
        request, session, current_user,
        booking_type="VM", page_path="/book/vm", active_nav="vm",
        filter=filter, show_released=show_released,
    )


@router.get("/book/namespace", response_class=HTMLResponse)
async def namespace_bookings_page(
    request: Request,
    filter: str = "mine",
    show_released: bool = False,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return await _render_bookings_page(
        request, session, current_user,
        booking_type="NAMESPACE", page_path="/book/namespace", active_nav="namespace",
        filter=filter, show_released=show_released,
    )


async def _render_form_error(request, session, current_user, booking_type="VM", **errors):
    """Re-render the booking form with an error banner (HTMX swaps the form area)."""
    ctx = {
        "vm_images": await _image_repo.list_active(session),
        "hw_configs": await _hw_config_repo.list_active(session),
        "available_namespaces": await _namespace_repo.list_available(session),
        "available_static_vms": await _static_vm_repo.list_available(session),
        "current_user": current_user,
        "booking_type": booking_type,
    }
    ctx.update(errors)
    return templates.TemplateResponse(
        request, "partials/booking_form.html", ctx,
        headers={"HX-Retarget": "#booking-form-area", "HX-Reswap": "outerHTML"},
    )


@router.post("/bookings", response_class=HTMLResponse)
async def create_booking(
    request: Request,
    ttl_minutes: int = Form(...),
    resource_type: str = Form("VM"),
    label: str = Form(""),
    image_id: UUID | None = Form(None),
    hw_config_id: UUID | None = Form(None),
    namespace_id: UUID | None = Form(None),
    static_vm_id: UUID | None = Form(None),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # ── Namespace booking — reserve from the pool (pick-specific or any), else queue ──
    if resource_type == ResourceType.NAMESPACE.value:
        try:
            booking = await _book_namespace_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), namespace_id=namespace_id
            )
        except NamespaceUnavailableError as exc:
            return await _render_form_error(
                request, session, current_user, booking_type="NAMESPACE", namespace_error=str(exc)
            )

    # ── Static VM booking — reserve from the pool, no provisioning ──
    elif resource_type == ResourceType.STATIC_VM.value:
        try:
            booking = await _reserve_static_vm_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), static_vm_id=static_vm_id
            )
        except StaticVMUnavailableError as exc:
            return await _render_form_error(
                request, session, current_user, static_vm_error=str(exc)
            )

    # ── VM booking — existing provisioning flow ──
    else:
        if image_id is None or hw_config_id is None:
            return await _render_form_error(
                request, session, current_user, quota_error="Select an image and hardware config",
            )
        try:
            booking = await _use_case.execute(
                session, ttl_minutes, image_id, hw_config_id,
                user_id=str(current_user.id), label=label.strip() or None,
            )
        except QuotaExceededError as exc:
            return await _render_form_error(request, session, current_user, quota_error=str(exc))

    booking.owner_username = current_user.username
    await _attach_queue_position(session, booking)
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}, status_code=201
    )


@router.get("/bookings/{booking_id}/row", response_class=HTMLResponse)
async def booking_row(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")

    await _attach_queue_position(session, booking)
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.delete("/bookings/{booking_id}", status_code=202, response_class=HTMLResponse)
async def release_booking(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _release_use_case.execute(session, booking_id, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}, status_code=202
    )


@router.put("/bookings/{booking_id}/extend", response_class=HTMLResponse)
async def extend_booking(
    booking_id: UUID,
    request: Request,
    extend_minutes: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _extend_use_case.execute(session, booking_id, extend_minutes, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.get("/bookings/{booking_id}/audit", response_class=HTMLResponse)
async def booking_audit_page(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Human-readable audit timeline for a booking (linked from a failed booking row)."""
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the booking owner")

    entries = await _repo.list_audit(session, booking_id)
    return templates.TemplateResponse(
        request, "audit_log.html",
        {"booking": booking, "entries": entries, "current_user": current_user},
    )
