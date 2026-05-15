from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.config import settings
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository

router = APIRouter()
templates = Jinja2Templates(directory="app/presentation/templates")

_repo = BookingRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_use_case = CreateBookingUseCase(_repo, _image_repo, _hw_config_repo)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_async_session)):
    bookings = await _repo.list_all(session)
    vm_images = await _image_repo.list_active(session)
    hw_configs = await _hw_config_repo.list_active(session)
    return templates.TemplateResponse(
        request, "index.html",
        {"bookings": bookings, "vm_images": vm_images, "hw_configs": hw_configs},
    )


@router.get("/bookings")
async def list_bookings(session: AsyncSession = Depends(get_async_session)):
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
):
    booking = await _use_case.execute(session, ttl_minutes, image_id, hw_config_id)

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
        request, "partials/booking_row.html", {"booking": booking}, status_code=201
    )


@router.get("/bookings/{booking_id}/row", response_class=HTMLResponse)
async def booking_row(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    booking = await _repo.get(session, booking_id)
    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking}
    )


_RELEASABLE_STATUSES = {BookingStatus.READY, BookingStatus.FAILED}
_IN_FLIGHT_STATUSES  = {BookingStatus.PENDING, BookingStatus.PROVISIONING, BookingStatus.RETRY, BookingStatus.RELEASING}


@router.delete("/bookings/{booking_id}", status_code=202)
async def release_booking(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    from app.tasks.teardown import teardown_vm_task  # avoid circular import at module load

    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status in _IN_FLIGHT_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot release an in-flight booking")

    if booking.status not in _RELEASABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Cannot release booking with status {booking.status.value}")

    await _repo.update_status(session, booking_id, BookingStatus.RELEASING, actor_id=settings.DEV_USER_ID)
    teardown_vm_task.delay(str(booking_id))

    booking = await _repo.get(session, booking_id)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"id": str(booking.id), "status": booking.status.value}, status_code=202)

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking}, status_code=202
    )


@router.get("/bookings/{booking_id}/audit")
async def get_booking_audit(
    booking_id: UUID,
    session: AsyncSession = Depends(get_async_session),
):
    try:
        await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

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
