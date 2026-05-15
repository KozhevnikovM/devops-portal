import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.presentation.routes.bookings import router
from app.presentation.routes.api import router as api_router
from app.tasks.provision import provision_vm_task


class _SuppressRowPolling(logging.Filter):
    """Drop uvicorn access-log entries for the frequent row-polling endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/row " not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_SuppressRowPolling())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _recover_in_progress_bookings()
    yield


def _recover_in_progress_bookings() -> None:
    """Re-queue provision tasks for bookings stuck in non-terminal states after a restart."""
    if settings.USE_STUB_TERRAFORM:
        return

    repo = BookingRepository()
    with SyncSessionLocal() as session:
        bookings = repo.sync_list_in_progress(session)

    if not bookings:
        logger.info("startup recovery: no in-progress bookings found")
        return

    for booking in bookings:
        provision_vm_task.delay(str(booking.id))
        logger.info(
            "startup recovery: re-queued provision task for booking %s (status=%s)",
            booking.id,
            booking.status.value,
        )

    logger.info("startup recovery: re-queued %d booking(s)", len(bookings))


app = FastAPI(title="DevOps Portal", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
app.include_router(api_router)
