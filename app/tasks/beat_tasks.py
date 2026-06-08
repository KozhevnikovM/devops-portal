import logging

from app.config import settings
from app.domain.enums import BookingStatus
from app.infrastructure.celery_app import celery_app
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository

logger = logging.getLogger(__name__)

repo = BookingRepository()


@celery_app.task
def enforce_ttl() -> None:
    """Queue teardown for every READY booking whose TTL has expired."""
    from app.tasks.teardown import teardown_vm_task

    with SyncSessionLocal() as session:
        expired = repo.sync_list_expired(session)

    logger.info("enforce_ttl: found %d expired booking(s)", len(expired))

    for booking in expired:
        try:
            with SyncSessionLocal() as session:
                repo.sync_update_status(session, booking.id, BookingStatus.RELEASING)
            teardown_vm_task.delay(str(booking.id))
            logger.info("enforce_ttl: queued teardown for booking %s", booking.id)
        except Exception:
            logger.exception("enforce_ttl: failed to queue teardown for booking %s", booking.id)


@celery_app.task
def reap_stale_provisioning() -> None:
    """Mark PENDING/PROVISIONING/CONFIGURING/RETRY bookings stuck past the threshold as FAILED."""
    threshold = settings.STALE_PROVISIONING_THRESHOLD_MINUTES

    with SyncSessionLocal() as session:
        stale = repo.sync_list_stale_provisioning(session, threshold_minutes=threshold)

    logger.info("reap_stale_provisioning: found %d stale booking(s)", len(stale))

    for booking in stale:
        try:
            with SyncSessionLocal() as session:
                repo.sync_update_status(session, booking.id, BookingStatus.FAILED)
            logger.warning(
                "reap_stale_provisioning: marked booking %s FAILED (stuck in %s > %d min)",
                booking.id, booking.status.value, threshold,
            )
        except Exception:
            logger.exception(
                "reap_stale_provisioning: failed to reap booking %s", booking.id
            )
