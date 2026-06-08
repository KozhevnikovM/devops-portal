import logging

from app.config import settings
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.celery_app import celery_app
from app.infrastructure.database.session import SyncSessionLocal
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository

logger = logging.getLogger(__name__)

repo = BookingRepository()
env_repo = EnvironmentRepository()


def _release_child_sync(session, booking, dispatch_teardown) -> None:
    """Release one environment child (sync): pooled → RELEASED + promote; VM → RELEASING + teardown."""
    if booking.resource_type in (ResourceType.NAMESPACE, ResourceType.STATIC_VM):
        repo.sync_update_status(session, booking.id, BookingStatus.RELEASED)
        repo.sync_promote_next_queued(session, booking.resource_type.value)
    elif booking.status == BookingStatus.QUEUED:
        repo.sync_update_status(session, booking.id, BookingStatus.RELEASED)
    else:
        repo.sync_update_status(session, booking.id, BookingStatus.RELEASING)
        dispatch_teardown(str(booking.id))


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


@celery_app.task
def enforce_environment_ttl() -> None:
    """Release expired environments as a group — tear down all their live children together."""
    from app.tasks.teardown import teardown_vm_task

    with SyncSessionLocal() as session:
        expired = env_repo.sync_list_expired(session)

    logger.info("enforce_environment_ttl: found %d expired environment(s)", len(expired))

    for env in expired:
        try:
            with SyncSessionLocal() as session:
                children = env_repo.sync_live_children(session, env.id)
                for child in children:
                    _release_child_sync(session, child, teardown_vm_task.delay)
            logger.info("enforce_environment_ttl: released environment %s (%d children)", env.id, len(children))
        except Exception:
            logger.exception("enforce_environment_ttl: failed to release environment %s", env.id)
