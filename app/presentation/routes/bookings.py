import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.create_booking import CreateBookingUseCase
from app.domain.enums import BookingStatus
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository

router = APIRouter()
templates = Jinja2Templates(directory="app/presentation/templates")

_repo = BookingRepository()
_use_case = CreateBookingUseCase(_repo)

TERMINAL_STATUSES = {BookingStatus.READY, BookingStatus.FAILED}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_async_session)):
    bookings = await _repo.list_all(session)
    return templates.TemplateResponse(request, "index.html", {"bookings": bookings})


@router.post("/bookings")
async def create_booking(
    request: Request,
    ttl_hours: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
):
    booking = await _use_case.execute(session, ttl_hours)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "id": str(booking.id),
                "status": booking.status.value,
                "ttl_hours": booking.ttl_hours,
                "expires_at": booking.expires_at.isoformat(),
                "created_at": booking.created_at.isoformat(),
            },
            status_code=201,
        )

    return templates.TemplateResponse(
        request, "partials/booking_row.html", {"booking": booking}, status_code=201
    )


@router.get("/bookings/{booking_id}/status-stream")
async def booking_status_stream(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            try:
                booking = await _repo.get(session, booking_id)
            except Exception:
                break

            html = templates.get_template("partials/booking_row.html").render(
                {"booking": booking}
            )
            yield f"data: {html}\n\n"

            if booking.status in TERMINAL_STATUSES:
                break

            await asyncio.sleep(2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
