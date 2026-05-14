from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.template_repo import TemplateRepository

router = APIRouter()
templates = Jinja2Templates(directory="app/presentation/templates")

_repo = BookingRepository()
_template_repo = TemplateRepository()
_use_case = CreateBookingUseCase(_repo, _template_repo)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_async_session)):
    bookings = await _repo.list_all(session)
    vm_templates = await _template_repo.list_active(session)
    return templates.TemplateResponse(
        request, "index.html", {"bookings": bookings, "vm_templates": vm_templates}
    )


@router.post("/bookings")
async def create_booking(
    request: Request,
    ttl_hours: int = Form(...),
    template_id: UUID = Form(...),
    session: AsyncSession = Depends(get_async_session),
):
    booking = await _use_case.execute(session, ttl_hours, template_id)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "id": str(booking.id),
                "status": booking.status.value,
                "ttl_hours": booking.ttl_hours,
                "expires_at": booking.expires_at.isoformat(),
                "created_at": booking.created_at.isoformat(),
                "template_id": str(booking.template_id),
                "template_name": booking.template_name,
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
