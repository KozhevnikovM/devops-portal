import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.presentation.routes.bookings import router
from app.presentation.routes.api import router as api_router


class _SuppressRowPolling(logging.Filter):
    """Drop uvicorn access-log entries for the frequent row-polling endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/row " not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_SuppressRowPolling())

app = FastAPI(title="DevOps Portal")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
app.include_router(api_router)
