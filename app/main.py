import logging
from contextlib import asynccontextmanager

import bcrypt
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.user_repo import UserRepository
from app.presentation.routes.admin import router as admin_router
from app.presentation.routes.auth import router as auth_router
from app.presentation.routes.bookings import router
from app.presentation.routes.api import router as api_router
from app.presentation.routes.api_bookings import router as api_bookings_router
from app.presentation.routes.api_environments import router as api_environments_router
from app.tasks.provision import provision_vm_task


class _SuppressRowPolling(logging.Filter):
    """Drop uvicorn access-log entries for the frequent row-polling endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/row " not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_SuppressRowPolling())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_admin_user()
    _recover_in_progress_bookings()
    yield


def _seed_admin_user() -> None:
    """Create the initial admin user if no users exist."""
    repo = UserRepository()
    with SyncSessionLocal() as session:
        if repo.sync_list_all(session):
            return

        pw_hash = bcrypt.hashpw(settings.ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        repo.sync_create(session, settings.ADMIN_USERNAME, pw_hash, "admin")
        logger.info("seeded admin user '%s'", settings.ADMIN_USERNAME)

    if settings.ADMIN_PASSWORD == "changeme":
        logger.warning("SECURITY: default admin password is still 'changeme' — change it")


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
        provision_vm_task.delay(str(booking.id), str(booking.image_id), str(booking.hw_config_id))
        logger.info(
            "startup recovery: re-queued provision task for booking %s (status=%s)",
            booking.id,
            booking.status.value,
        )

    logger.info("startup recovery: re-queued %d booking(s)", len(bookings))


app = FastAPI(
    title="DevOps Portal",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
    root_path=settings.ROOT_PATH,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(router)
app.include_router(api_router)
app.include_router(api_bookings_router)
app.include_router(api_environments_router)

# Keep the OpenAPI schema (/docs) to the JSON API surface: hide the HTML/HTMX page and
# fragment routes, which all declare response_class=HTMLResponse. get_openapi() skips routes
# with include_in_schema=False, so this also covers any HTML route added later.
for _route in app.routes:
    if isinstance(_route, APIRoute) and _route.response_class is HTMLResponse:
        _route.include_in_schema = False


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "dp_<api_key>",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _custom_openapi
