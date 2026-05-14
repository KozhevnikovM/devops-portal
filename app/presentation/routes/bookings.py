from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
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


@router.post("/bookings")
async def create_booking(
    request: Request,
    ttl_hours: int = Form(...),
    image_id: UUID = Form(...),
    hw_config_id: UUID = Form(...),
    session: AsyncSession = Depends(get_async_session),
):
    booking = await _use_case.execute(session, ttl_hours, image_id, hw_config_id)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "id": str(booking.id),
                "status": booking.status.value,
                "ttl_hours": booking.ttl_hours,
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
