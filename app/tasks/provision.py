import asyncio
import logging
from uuid import UUID

from app.config import settings
from app.domain.enums import BookingStatus
from app.infrastructure.celery_app import celery_app
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.terraform.stub_adapter import StubTerraformAdapter
from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter

logger = logging.getLogger(__name__)

VM_TEMPLATE_CONFIG = {
    "cpus":      1,
    "memory":    2048,   # MB
    "disk_size": 13312,  # MB (13 × 1024)
}

repo = BookingRepository()
terraform = StubTerraformAdapter() if settings.USE_STUB_TERRAFORM else TerraformVcdAdapter()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=120, rate_limit="0.5/m")
def provision_vm_task(self, booking_id: str) -> None:
    booking_uuid = UUID(booking_id)
    workspace_id = f"booking-{booking_id}"

    config = {**VM_TEMPLATE_CONFIG, "name": f"portal-{booking_id[:8]}"}

    with SyncSessionLocal() as session:
        try:
            repo.sync_update_status(session, booking_uuid, BookingStatus.PROVISIONING)
            logger.info("Provisioning started for booking %s", booking_id)

            result = asyncio.run(terraform.apply(workspace_id, config))
            ip = result["ip"]

            repo.sync_update_status(session, booking_uuid, BookingStatus.READY, vm_ip=ip)
            logger.info("Provisioning complete for booking %s — IP: %s", booking_id, ip)

        except Exception as exc:
            logger.error("Provisioning failed for booking %s: %s", booking_id, exc)
            try:
                repo.sync_update_status(session, booking_uuid, BookingStatus.FAILED)
            except Exception:
                pass
            raise self.retry(exc=exc)
