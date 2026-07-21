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
from app.presentation.middleware.csrf_origin import CSRFOriginMiddleware
from app.presentation.routes.admin import router as admin_router
from app.presentation.routes.auth import router as auth_router
from app.presentation.routes.bookings import router
from app.presentation.routes.api import router as api_router
from app.presentation.routes.api_bookings import router as api_bookings_router
from app.presentation.routes.api_environments import router as api_environments_router
from app.presentation.routes.environments import router as environments_router
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

        if not settings.ADMIN_PASSWORD:
            if not settings.USE_STUB_TERRAFORM:
                raise RuntimeError(
                    "ADMIN_PASSWORD must be set in production (USE_STUB_TERRAFORM=False). "
                    "Set it in your .env or vault."
                )
            effective_pw = "changeme"
            logger.warning("ADMIN_PASSWORD not set — using 'changeme' (dev/stub mode only)")
        else:
            effective_pw = settings.ADMIN_PASSWORD

        pw_hash = bcrypt.hashpw(effective_pw.encode(), bcrypt.gensalt()).decode()
        repo.sync_create(session, settings.ADMIN_USERNAME, pw_hash, "admin")
        logger.info("seeded admin user '%s'", settings.ADMIN_USERNAME)


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
app.add_middleware(CSRFOriginMiddleware, base_url=settings.BASE_URL)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(router)
# Legacy /api/... — preserved for backward compat, hidden from OpenAPI docs
app.include_router(api_router, prefix="/api", include_in_schema=False)
app.include_router(api_bookings_router, prefix="/api", include_in_schema=False)
app.include_router(api_environments_router, prefix="/api", include_in_schema=False)
# Canonical /api/v1/... — the stable versioned surface
app.include_router(api_router, prefix="/api/v1")
app.include_router(api_bookings_router, prefix="/api/v1")
app.include_router(api_environments_router, prefix="/api/v1")
app.include_router(environments_router)

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


@app.get("/health", tags=["platform"], summary="Liveness probe")
async def health():
    return {"status": "ok"}
