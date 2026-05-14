import asyncio
import logging
from uuid import UUID

from app.config import settings
from app.domain.enums import BookingStatus
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


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def teardown_vm_task(self, booking_id: str) -> None:
    booking_uuid = UUID(booking_id)
    workspace_id = f"booking-{booking_id}"

    with SyncSessionLocal() as session:
        try:
            booking = repo.sync_get(session, booking_uuid)
            image = image_repo.sync_get(session, booking.image_id)
            hw = hw_config_repo.sync_get(session, booking.hw_config_id)
            config = {
                "name":             f"portal-{booking_id[:8]}",
                "vapp_template_id": image.vapp_template_id,
                "cpus":             hw.cpus,
                "memory":           hw.memory_mb,
                "disk_size":        hw.disk_mb,
            }

            repo.sync_update_status(session, booking_uuid, BookingStatus.RELEASING)
            logger.info("Teardown started for booking %s", booking_id)

            asyncio.run(terraform.destroy(workspace_id, config))

            repo.sync_update_status(session, booking_uuid, BookingStatus.RELEASED)
            logger.info("Teardown complete for booking %s", booking_id)

        except Exception as exc:
            logger.error("Teardown failed for booking %s: %s", booking_id, exc)
            is_last_attempt = self.request.retries >= self.max_retries
            if is_last_attempt:
                try:
                    repo.sync_update_status(session, booking_uuid, BookingStatus.FAILED)
                except Exception:
                    pass
            raise self.retry(exc=exc)
