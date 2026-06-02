from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.domain.entities import User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingError, BookingNotFoundError, PermissionError, QuotaExceededError
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.presentation.templating import templates

router = APIRouter()

_repo = BookingRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_use_case = CreateBookingUseCase(_repo, _image_repo, _hw_config_repo)
_extend_use_case = ExtendBookingUseCase(_repo)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    filter: str = "mine",
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if filter == "all":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
    vm_images = await _image_repo.list_active(session)
    hw_configs = await _hw_config_repo.list_active(session)
    return templates.TemplateResponse(
        request, "index.html",
        {
            "bookings": bookings,
            "vm_images": vm_images,
            "hw_configs": hw_configs,
            "current_user": current_user,
            "active_filter": filter,
        },
    )


@router.get("/bookings")
async def list_bookings(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    bookings = await _repo.list_all(session)
    return JSONResponse([
        {
            "id": str(b.id),
            "user_id": b.user_id,
            "status": b.status.value,
            "ttl_minutes": b.ttl_minutes,
            "expires_at": b.expires_at.isoformat(),
            "created_at": b.created_at.isoformat(),
            "image_id": str(b.image_id),
            "image_name": b.image_name,
            "hw_config_id": str(b.hw_config_id),
            "hw_config_name": b.hw_config_name,
            "vm_ip": b.vm_ip,
            "vm_password": b.vm_password,
        }
        for b in bookings
    ])


@router.post("/bookings")
async def create_booking(
    request: Request,
    ttl_minutes: int = Form(...),
    image_id: UUID = Form(...),
    hw_config_id: UUID = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _use_case.execute(session, ttl_minutes, image_id, hw_config_id, user_id=str(current_user.id))
    except QuotaExceededError as exc:
        if "application/json" in request.headers.get("accept", ""):
            raise HTTPException(status_code=409, detail=str(exc))
        vm_images = await _image_repo.list_active(session)
        hw_configs = await _hw_config_repo.list_active(session)
        return templates.TemplateResponse(
            request, "partials/booking_form.html",
            {
                "vm_images": vm_images,
                "hw_configs": hw_configs,
                "current_user": current_user,
                "quota_error": str(exc),
            },
            headers={"HX-Retarget": "#booking-form-area", "HX-Reswap": "outerHTML"},
        )

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "id": str(booking.id),
                "status": booking.status.value,
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
    booking = await _repo.get(session, booking_id)
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

    if not is_admin_force_delete:
        if booking.status in _IN_FLIGHT_STATUSES:
            raise HTTPException(status_code=409, detail="Cannot release an in-flight booking")
        if booking.status not in _RELEASABLE_STATUSES:
            raise HTTPException(status_code=409, detail=f"Cannot release booking with status {booking.status.value}")

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
    except PermissionError as exc:
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
