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
                # A failed child has settled — start its environment's lease if due (#434).
                env_repo.sync_start_lease_if_ready_for_booking(session, booking.id)
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


@celery_app.task
def reconcile_environment_leases() -> None:
    """Start the lease for every settled environment still on the placeholder expiry (#434).

    The immediate lease triggers (provision READY/FAILED, the stale reaper, queue promotion) each
    run in their own transaction after the settling commit, so a crash in between would leave the
    stack unbounded. This sweep is the guarantee; it also repairs environments stuck before #434.
    """
    with SyncSessionLocal() as session:
        pending = env_repo.sync_list_lease_pending(session)

    logger.info("reconcile_environment_leases: found %d environment(s) awaiting a lease", len(pending))

    for environment_id in pending:
        try:
            with SyncSessionLocal() as session:
                if env_repo.sync_start_lease_if_ready(session, environment_id):
                    logger.info("reconcile_environment_leases: started lease for environment %s", environment_id)
        except Exception:
            logger.exception("reconcile_environment_leases: failed for environment %s", environment_id)
