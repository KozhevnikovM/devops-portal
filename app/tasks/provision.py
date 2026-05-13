import asyncio
import logging
import time
from uuid import UUID

import redis as redis_lib

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


def _token_pool() -> list[str]:
    if settings.VCD_API_TOKENS:
        return [t.strip() for t in settings.VCD_API_TOKENS.split(",") if t.strip()]
    if settings.VCD_API_TOKEN:
        return [settings.VCD_API_TOKEN]
    return []


def _acquire_token(tokens: list[str], redis_client, timeout: int = 60) -> tuple[int, str]:
    """Poll until one token lock is acquired; return (index, token)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for i, token in enumerate(tokens):
            if redis_client.set(
                f"vcd_token_lock:{i}", "1",
                nx=True, ex=settings.VCD_TOKEN_LOCK_TTL,
            ):
                return i, token
        time.sleep(5)
    raise RuntimeError(f"no VCD token available after {timeout}s")


@celery_app.task(
    bind=True,
    max_retries=settings.PROVISION_MAX_RETRIES,
    default_retry_delay=settings.PROVISION_RETRY_DELAY,
    rate_limit=settings.PROVISION_RATE_LIMIT,
)
def provision_vm_task(self, booking_id: str) -> None:
    booking_uuid = UUID(booking_id)
    workspace_id = f"booking-{booking_id}"
    config = {**VM_TEMPLATE_CONFIG, "name": f"portal-{booking_id[:8]}"}

    tokens = _token_pool()
    use_semaphore = not settings.USE_STUB_TERRAFORM and bool(tokens)

    lock_key = None
    redis_client = None
    api_token = None

    if use_semaphore:
        redis_client = redis_lib.Redis.from_url(settings.REDIS_URL)
        try:
            token_index, api_token = _acquire_token(tokens, redis_client)
            lock_key = f"vcd_token_lock:{token_index}"
            logger.info("Acquired token lock %s for booking %s", lock_key, booking_id)
        except RuntimeError as exc:
            logger.warning("Token acquire timed out for booking %s, retrying", booking_id)
            raise self.retry(exc=exc)

    try:
        with SyncSessionLocal() as session:
            try:
                repo.sync_update_status(session, booking_uuid, BookingStatus.PROVISIONING)
                logger.info("Provisioning started for booking %s", booking_id)

                result = asyncio.run(terraform.apply(workspace_id, config, api_token=api_token))
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
    finally:
        if redis_client and lock_key:
            redis_client.delete(lock_key)
            logger.info("Released token lock %s for booking %s", lock_key, booking_id)
