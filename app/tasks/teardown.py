import asyncio
import logging
from uuid import UUID

from app.config import settings
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.celery_app import celery_app
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter

logger = logging.getLogger(__name__)

repo = BookingRepository()
image_repo = ImageRepository()
hw_config_repo = HWConfigRepository()
terraform = StubTerraformAdapter() if settings.USE_STUB_TERRAFORM else TerraformVcdAdapter()


def _run(work):
    """Run one unit of DB work in a short-lived session, releasing the connection immediately.

    Mirrors provision._run() — keeps the worker from pinning a pool connection across
    the minutes-long terraform destroy.
    """
    with SyncSessionLocal() as session:
        return work(session)


def _any_api_token() -> str | None:
    """Return any available VCD API token for destroy (no locking needed)."""
    if settings.VCD_API_TOKENS:
        tokens = [t.strip() for t in settings.VCD_API_TOKENS.split(",") if t.strip()]
        if tokens:
            return tokens[0]
    return settings.VCD_API_TOKEN or None


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def teardown_vm_task(self, booking_id: str, force: bool = False) -> None:
    booking_uuid = UUID(booking_id)
    workspace_id = f"booking-{booking_id}"
    api_token = None if settings.USE_STUB_TERRAFORM else _any_api_token()

    # Read minimum data needed; release the connection before terraform runs.
    booking = _run(lambda s: repo.sync_get(s, booking_uuid))

    # Pooled resources (namespaces, static VMs) aren't provisioned —
    # releasing returns them to the pool, then promotes the next queued booking.
    if booking.resource_type in (ResourceType.NAMESPACE, ResourceType.STATIC_VM):
        _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASED))
        _run(lambda s: repo.sync_promote_next_queued(s, booking.resource_type.value))
        logger.info("Released pooled booking %s (%s)", booking_id, booking.resource_type.value)
        return

    image = _run(lambda s: image_repo.sync_get(s, booking.image_id))
    hw = _run(lambda s: hw_config_repo.sync_get(s, booking.hw_config_id))
    config = {
        "name":             f"portal-{booking_id[:8]}",
        "vapp_template_id": image.vapp_template_id,
        "cpus":             hw.cpus,
        "memory":           hw.memory_mb,
        "disk_size":        hw.disk_mb,
        "vm_password":      booking.vm_password or "",
    }

    try:
        _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASING))
        logger.info("Teardown started for booking %s (force=%s)", booking_id, force)

        def _on_progress(msg: str) -> None:
            # Each progress write gets its own short-lived session/connection.
            _run(lambda s: repo.sync_set_status_message(s, booking_uuid, msg))

        # No DB connection is held across the (minutes-long) terraform destroy.
        asyncio.run(terraform.destroy(workspace_id, config, api_token, on_progress=_on_progress, force=force))

        _run(lambda s: repo.sync_set_status_message(s, booking_uuid, None))
        _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASED))
        logger.info("Teardown complete for booking %s", booking_id)

    except Exception as exc:
        logger.error("Teardown failed for booking %s: %s", booking_id, exc)
        if force:
            logger.warning("Force teardown: marking booking %s as RELEASED despite error", booking_id)
            try:
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, None))
                _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.RELEASED))
            except Exception:
                pass
            return
        is_last_attempt = self.request.retries >= self.max_retries
        if is_last_attempt:
            try:
                _run(lambda s: repo.sync_set_status_message(s, booking_uuid, "Teardown failed — see audit log"))
                _run(lambda s: repo.sync_update_status(s, booking_uuid, BookingStatus.FAILED))
            except Exception:
                pass
        raise self.retry(exc=exc)
