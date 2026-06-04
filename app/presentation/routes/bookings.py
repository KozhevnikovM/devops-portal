from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase
from app.domain.entities import User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError, BookingNotFoundError, NamespaceUnavailableError, BookingPermissionError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository
from app.presentation.templating import templates

router = APIRouter()

_repo = BookingRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_namespace_repo = NamespaceRepository()
_static_vm_repo = StaticVMRepository()
_use_case = CreateBookingUseCase(_repo, _image_repo, _hw_config_repo)
_extend_use_case = ExtendBookingUseCase(_repo)
_book_namespace_use_case = BookNamespaceUseCase(_repo, _namespace_repo)
_reserve_static_vm_use_case = ReserveStaticVMUseCase(_repo, _static_vm_repo)

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
        booking_type="VM", page_path="/", active_nav="vm",
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


@router.get("/bookings")
async def list_bookings(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # Owner-scoped: non-admins see only their own bookings; admins see all.
    # Secrets (vm_password / static-VM credentials) are never vended here — only on the
    # owner-scoped creation response and the owner/admin-gated single-row view.
    if current_user.role == "admin":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
    return JSONResponse([
        {
            "id": str(b.id),
            "user_id": b.user_id,
            "status": b.status.value,
            "resource_type": b.resource_type.value,
            "ttl_minutes": b.ttl_minutes,
            "expires_at": b.expires_at.isoformat(),
            "created_at": b.created_at.isoformat(),
            "image_id": str(b.image_id) if b.image_id else None,
            "image_name": b.image_name,
            "hw_config_id": str(b.hw_config_id) if b.hw_config_id else None,
            "hw_config_name": b.hw_config_name,
            "vm_ip": b.vm_ip,
            "namespace": b.namespace_name,
            "cluster": b.cluster_name,
            "api_url": b.api_url,
            "static_vm": b.static_vm_name,
            "host": b.static_vm_host,
            "username": b.static_vm_username,
        }
        for b in bookings
    ])


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


@router.post("/bookings")
async def create_booking(
    request: Request,
    ttl_minutes: int = Form(...),
    resource_type: str = Form("VM"),
    image_id: UUID | None = Form(None),
    hw_config_id: UUID | None = Form(None),
    namespace_id: UUID | None = Form(None),
    static_vm_id: UUID | None = Form(None),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    wants_json = "application/json" in request.headers.get("accept", "")

    # ── Namespace booking — reserve from the pool (pick-specific or any), else queue ──
    if resource_type == ResourceType.NAMESPACE.value:
        try:
            booking = await _book_namespace_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), namespace_id=namespace_id
            )
        except NamespaceUnavailableError as exc:
            if wants_json:
                raise HTTPException(status_code=409, detail=str(exc))
            return await _render_form_error(
                request, session, current_user, booking_type="NAMESPACE", namespace_error=str(exc)
            )

        booking.owner_username = current_user.username
        await _attach_queue_position(session, booking)
        if wants_json:
            return JSONResponse(
                {
                    "id": str(booking.id),
                    "status": booking.status.value,
                    "resource_type": booking.resource_type.value,
                    "ttl_minutes": booking.ttl_minutes,
                    "expires_at": booking.expires_at.isoformat(),
                    "created_at": booking.created_at.isoformat(),
                    "namespace": booking.namespace_name,
                    "cluster": booking.cluster_name,
                    "api_url": booking.api_url,
                    "queue_position": booking.queue_position,
                },
                status_code=201,
            )
        return templates.TemplateResponse(
            request, "partials/booking_row.html",
            {"booking": booking, "current_user": current_user}, status_code=201,
        )

    # ── Static VM booking — reserve from the pool, no provisioning ──
    if resource_type == ResourceType.STATIC_VM.value:
        try:
            booking = await _reserve_static_vm_use_case.execute(
                session, ttl_minutes, user_id=str(current_user.id), static_vm_id=static_vm_id
            )
        except StaticVMUnavailableError as exc:
            if wants_json:
                raise HTTPException(status_code=409, detail=str(exc))
            return await _render_form_error(
                request, session, current_user, static_vm_error=str(exc)
            )

        booking.owner_username = current_user.username
        await _attach_queue_position(session, booking)
        if wants_json:
            return JSONResponse(
                {
                    "id": str(booking.id),
                    "status": booking.status.value,
                    "resource_type": booking.resource_type.value,
                    "ttl_minutes": booking.ttl_minutes,
                    "expires_at": booking.expires_at.isoformat(),
                    "created_at": booking.created_at.isoformat(),
                    "static_vm": booking.static_vm_name,
                    "host": booking.static_vm_host,
                    "username": booking.static_vm_username,
                    "password": booking.static_vm_password,
                    "ssh_key": booking.static_vm_ssh_key,
                    "queue_position": booking.queue_position,
                },
                status_code=201,
            )
        return templates.TemplateResponse(
            request, "partials/booking_row.html",
            {"booking": booking, "current_user": current_user}, status_code=201,
        )

    # ── VM booking — existing provisioning flow ──
    if image_id is None or hw_config_id is None:
        if wants_json:
            raise HTTPException(status_code=400, detail="image_id and hw_config_id are required")
        return await _render_form_error(
            request, session, current_user, quota_error="Select an image and hardware config",
        )

    try:
        booking = await _use_case.execute(session, ttl_minutes, image_id, hw_config_id, user_id=str(current_user.id))
    except QuotaExceededError as exc:
        if wants_json:
            raise HTTPException(status_code=409, detail=str(exc))
        return await _render_form_error(request, session, current_user, quota_error=str(exc))

    if wants_json:
        return JSONResponse(
            {
                "id": str(booking.id),
                "status": booking.status.value,
                "resource_type": booking.resource_type.value,
                "ttl_minutes": booking.ttl_minutes,
                "expires_at": booking.expires_at.isoformat(),
                "created_at": booking.created_at.isoformat(),
                "image_id": str(booking.image_id),
                "image_name": booking.image_name,
                "hw_config_id": str(booking.hw_config_id),
                "hw_config_name": booking.hw_config_name,
            },
            status_code=201,
        )

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

    if booking.user_id != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not the booking owner")

    await _attach_queue_position(session, booking)
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


_RELEASABLE_STATUSES     = {BookingStatus.READY, BookingStatus.FAILED}
_FORCE_DELETABLE_STATUSES = {BookingStatus.PENDING, BookingStatus.PROVISIONING, BookingStatus.RETRY}
_IN_FLIGHT_STATUSES       = {*_FORCE_DELETABLE_STATUSES, BookingStatus.RELEASING}


@router.delete("/bookings/{booking_id}", status_code=202)
async def release_booking(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    from app.tasks.teardown import teardown_vm_task  # avoid circular import at module load

    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.user_id != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not the booking owner")

    is_admin_force_delete = current_user.role == "admin" and booking.status in _FORCE_DELETABLE_STATUSES

    if booking.status == BookingStatus.QUEUED:
        # Cancel the queue slot — holds no resource, so nothing to tear down or promote.
        await _repo.update_status(session, booking_id, BookingStatus.RELEASED, actor_id=str(current_user.id))
    else:
        if not is_admin_force_delete:
            if booking.status in _IN_FLIGHT_STATUSES:
                raise HTTPException(status_code=409, detail="Cannot release an in-flight booking")
            if booking.status not in _RELEASABLE_STATUSES:
                raise HTTPException(status_code=409, detail=f"Cannot release booking with status {booking.status.value}")

        if booking.resource_type in (ResourceType.NAMESPACE, ResourceType.STATIC_VM):
            # Pooled resource — return it to the pool, then hand it to the next queued booking.
            await _repo.update_status(session, booking_id, BookingStatus.RELEASED, actor_id=str(current_user.id))
            await _repo.promote_next_queued(session, booking.resource_type.value)
        else:
            await _repo.update_status(session, booking_id, BookingStatus.RELEASING, actor_id=str(current_user.id))
            teardown_vm_task.delay(str(booking_id))

    booking = await _repo.get(session, booking_id)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"id": str(booking.id), "status": booking.status.value}, status_code=202)

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}, status_code=202
    )


@router.put("/bookings/{booking_id}/extend")
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

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({
            "id": str(booking.id),
            "status": booking.status.value,
            "ttl_minutes": booking.ttl_minutes,
            "expires_at": booking.expires_at.isoformat(),
        })

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking, "current_user": current_user}
    )


@router.get("/bookings/{booking_id}/audit")
async def get_booking_audit(
    booking_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.user_id != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not the booking owner")

    entries = await _repo.list_audit(session, booking_id)
    return JSONResponse([
        {
            "id": str(e.id),
            "booking_id": str(e.booking_id),
            "action": e.action,
            "old_status": e.old_status,
            "new_status": e.new_status,
            "actor_id": e.actor_id,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ])
