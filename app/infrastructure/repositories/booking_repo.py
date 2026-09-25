import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import cast, Connection, Engine, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, aliased

from app.domain.booking_status import LIVE_STATUSES, can_transition
from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError, IllegalStatusTransitionError
from app.domain.lease import Lease
from app.domain.resource_details import (
    NamespaceDetails, ResourceFootprint, StaticVMDetails, VMDetails,
)
from app.infrastructure.database.models import (
    BookingAuditModel, BookingModel, EnvironmentModel, NamespaceModel, StaticVMModel, UserModel,
)
from app.infrastructure.events import (
    Routing,
    apublish_row_changed,
    publish_progress_changed,
    publish_row_changed,
)

# Second alias of users to resolve created_by (the dispatcher) → username, distinct from the
# owner join on user_id.
_CreatorUser = aliased(UserModel)


logger = logging.getLogger(__name__)



def _check_transition(old_value: str, new: BookingStatus, booking_id: UUID) -> None:
    """Raise IllegalStatusTransitionError on a disallowed move; raise ValueError on unknown
    stored status (fail-closed — I7). A no-op (old == new) is always allowed."""
    try:
        old = BookingStatus(old_value)
    except ValueError:
        raise ValueError(
            f"Booking {booking_id} has unrecognised status {old_value!r} in the database"
        )
    if old != new and not can_transition(old, new):
        raise IllegalStatusTransitionError(
            f"Cannot move booking {booking_id} from {old.value} to {new.value}"
        )


def _routing(model: BookingModel) -> Routing:
    return Routing(owner_id=model.user_id, created_by=model.created_by)


def _environment_routing_stmt(environment_id: UUID):
    return select(EnvironmentModel.user_id, EnvironmentModel.created_by).where(
        EnvironmentModel.id == environment_id
    )


async def environment_routing(
    bind: AsyncEngine | AsyncConnection, environment_id: UUID,
) -> Routing | None:
    """The environment's own owner/creator (#442), or None if it no longer exists.

    Not derivable from a child booking: an adopted namespace booking keeps its own ``created_by``
    while the environment records whoever ordered it.

    Read in its own short-lived session (PR #463 review), never the caller's: a failure there can
    never force a rollback that expires the caller's instances, and a success never leaves the
    caller's session in an open transaction holding a pool connection while the publish then
    waits on Redis. The session is closed — connection back in the pool — before this returns.
    """
    async with AsyncSession(bind) as session:
        row = (await session.execute(_environment_routing_stmt(environment_id))).one_or_none()
    return Routing(owner_id=row.user_id, created_by=row.created_by) if row is not None else None


def sync_environment_routing(bind: Engine | Connection, environment_id: UUID) -> Routing | None:
    """Sync twin of ``environment_routing`` — Celery worker path."""
    with Session(bind) as session:
        row = session.execute(_environment_routing_stmt(environment_id)).one_or_none()
    return Routing(owner_id=row.user_id, created_by=row.created_by) if row is not None else None


async def _apublish_lifecycle(session: AsyncSession, model: BookingModel) -> None:
    """Publish a lifecycle row-changed notification for ``model`` right after its commit.

    Everything the notification needs from the booking is captured *before* the environment
    lookup, so nothing afterwards can touch ``model`` (or the caller's session) again. For an
    environment child, the environment's routing is read in a separate short-lived session.
    Best-effort like the publish itself: a failed lookup is logged and the notification goes out
    without environment routing (subscribers then authorize that row from the DB) — never failing
    the committed write.
    """
    booking_id, booking_routing = model.id, _routing(model)
    environment_id = getattr(model, "environment_id", None)
    environment_routing_ = None
    if environment_id is not None:
        try:
            environment_routing_ = await environment_routing(session.bind, environment_id)
        except Exception:
            logger.exception("Failed to read routing for environment %s", environment_id)
    await apublish_row_changed(
        booking_id=booking_id, booking_routing=booking_routing,
        environment_id=environment_id, environment_routing=environment_routing_,
    )


def _publish_lifecycle(session: Session, model: BookingModel) -> None:
    """Sync twin of ``_apublish_lifecycle`` — Celery worker path."""
    booking_id, booking_routing = model.id, _routing(model)
    environment_id = getattr(model, "environment_id", None)
    environment_routing_ = None
    if environment_id is not None:
        try:
            environment_routing_ = sync_environment_routing(session.get_bind(), environment_id)
        except Exception:
            logger.exception("Failed to read routing for environment %s", environment_id)
    publish_row_changed(
        booking_id=booking_id, booking_routing=booking_routing,
        environment_id=environment_id, environment_routing=environment_routing_,
    )


def _to_audit_entity(m: BookingAuditModel) -> BookingAuditEntry:
    return BookingAuditEntry(
        id=m.id,
        booking_id=m.booking_id,
        actor_id=m.actor_id,
        action=m.action,
        old_status=m.old_status,
        new_status=m.new_status,
        metadata=m.extra,
        created_at=m.created_at,
    )


def _to_entity(
    m: BookingModel,
    owner_username: str | None = None,
    namespace: NamespaceModel | None = None,
    static_vm: StaticVMModel | None = None,
    created_by_username: str | None = None,
) -> Booking:
    try:
        _status = BookingStatus(m.status)
    except ValueError:
        raise ValueError(f"Booking {m.id} has unrecognised status {m.status!r} in the database")
    _resource_type = ResourceType(m.resource_type)

    if _resource_type == ResourceType.VM:
        _details: VMDetails | NamespaceDetails | StaticVMDetails | None = VMDetails(
            image_id=m.image_id,
            image_name=m.image_name,
            hw_config_id=m.hw_config_id,
            hw_config_name=m.hw_config_name,
            vm_ip=m.vm_ip,
            vm_password=m.vm_password,
            startup_script=m.startup_script,
            config_roles=tuple(m.config_roles or []),
            extra_vars=dict(m.extra_vars or {}),
            config_failed=m.config_failed,
        )
    elif _resource_type == ResourceType.NAMESPACE:
        _details = NamespaceDetails(
            namespace_id=m.namespace_id,
            namespace_name=namespace.name if namespace else None,
            cluster_name=namespace.cluster_name if namespace else None,
            api_url=namespace.api_url if namespace else None,
        )
    elif _resource_type == ResourceType.STATIC_VM:
        _details = StaticVMDetails(
            static_vm_id=m.static_vm_id,
            static_vm_name=static_vm.name if static_vm else None,
            static_vm_host=static_vm.host if static_vm else None,
            static_vm_username=static_vm.username if static_vm else None,
            static_vm_password=static_vm.password if static_vm else None,
            static_vm_ssh_key=static_vm.ssh_key if static_vm else None,
        )
    else:
        _details = None

    _footprint = ResourceFootprint(
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        disk_mb=m.disk_mb,
        drive_type=m.drive_type,
    )

    return Booking(
        id=m.id,
        user_id=m.user_id,
        status=_status,
        resource_type=_resource_type,
        ttl_minutes=m.ttl_minutes,
        expires_at=m.expires_at,
        created_at=m.created_at,
        image_id=m.image_id,
        image_name=m.image_name,
        hw_config_id=m.hw_config_id,
        hw_config_name=m.hw_config_name,
        vm_ip=m.vm_ip,
        vm_password=m.vm_password,
        owner_username=owner_username,
        cpus=m.cpus,
        memory_mb=m.memory_mb,
        disk_mb=m.disk_mb,
        drive_type=m.drive_type,
        status_message=m.status_message,
        provisioning_log=m.provisioning_log,
        startup_script=m.startup_script,
        config_roles=m.config_roles or [],
        extra_vars=m.extra_vars or {},
        config_failed=m.config_failed,
        environment_id=m.environment_id,
        environment_label=m.environment_label,
        label=m.label,
        created_by=m.created_by,
        created_by_username=created_by_username,
        namespace_id=m.namespace_id,
        namespace_name=namespace.name if namespace else None,
        cluster_name=namespace.cluster_name if namespace else None,
        api_url=namespace.api_url if namespace else None,
        static_vm_id=m.static_vm_id,
        static_vm_name=static_vm.name if static_vm else None,
        static_vm_host=static_vm.host if static_vm else None,
        static_vm_username=static_vm.username if static_vm else None,
        static_vm_password=static_vm.password if static_vm else None,
        static_vm_ssh_key=static_vm.ssh_key if static_vm else None,
        details=_details,
        footprint=_footprint,
    )


def _apply_resource_type_filter(stmt, resource_type: str | list[str] | None):
    """Filter by a single resource_type or any of a list (VM page wants VM + STATIC_VM)."""
    if resource_type is None:
        return stmt
    if isinstance(resource_type, (list, tuple, set)):
        return stmt.where(BookingModel.resource_type.in_(list(resource_type)))
    return stmt.where(BookingModel.resource_type == resource_type)


def _apply_label_filter(stmt, label: str | None):
    """Case-insensitive substring match on the booking's label; no-op if blank/None."""
    if label is None or not label.strip():
        return stmt
    return stmt.where(BookingModel.label.ilike(f"%{label.strip()}%"))


_POOLED_LIVE_STATUSES = [s.value for s in LIVE_STATUSES]

# Pooled resource model + the booking FK that references it, keyed by resource_type.
_POOLED_RESOURCE = {
    ResourceType.STATIC_VM.value: (StaticVMModel, BookingModel.static_vm_id),
    ResourceType.NAMESPACE.value: (NamespaceModel, BookingModel.namespace_id),
}


def _free_resource_stmt(resource_type: str):
    """Next free resource of a pooled type, lockable (FOR UPDATE SKIP LOCKED)."""
    model, fk = _POOLED_RESOURCE[resource_type]
    held = select(fk).where(fk.is_not(None), BookingModel.status.in_(_POOLED_LIVE_STATUSES))
    return (
        select(model)
        .where(model.is_active.is_(True), model.id.not_in(held))
        .order_by(model.name)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _oldest_queued_stmt(resource_type: str):
    """Oldest QUEUED booking of a type, lockable so two frees can't promote the same one."""
    return (
        select(BookingModel)
        .where(
            BookingModel.resource_type == resource_type,
            BookingModel.status == BookingStatus.QUEUED.value,
        )
        .order_by(BookingModel.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _queue_rank_stmt(resource_type: str, created_at: datetime):
    return select(func.count(BookingModel.id)).where(
        BookingModel.resource_type == resource_type,
        BookingModel.status == BookingStatus.QUEUED.value,
        BookingModel.created_at < created_at,
    )


def _assign_resource_and_ready(session, booking_model, resource_type: str, resource) -> None:
    """Attach a freed resource to a QUEUED booking, flip it READY, start its TTL, audit."""
    _, fk = _POOLED_RESOURCE[resource_type]
    setattr(booking_model, fk.key, resource.id)
    old_status = booking_model.status
    _check_transition(old_status, BookingStatus.READY, booking_model.id)
    booking_model.status = BookingStatus.READY.value
    booking_model.expires_at = Lease.starting_now(booking_model.ttl_minutes).expires_at
    session.add(BookingAuditModel(
        booking_id=booking_model.id,
        actor_id="system",
        action="STATUS_CHANGED",
        old_status=old_status,
        new_status=BookingStatus.READY.value,
    ))


def _environment_repo():
    """A promoted environment child may be the last to settle, so promotion also checks the
    environment's lease. Imported lazily: environment_repo imports this module."""
    from app.infrastructure.repositories.environment_repo import EnvironmentRepository
    return EnvironmentRepository()


class BookingRepository:
    async def create(self, session: AsyncSession, booking: Booking) -> Booking:
        model = BookingModel(
            id=booking.id,
            user_id=booking.user_id,
            status=booking.status.value,
            ttl_minutes=booking.ttl_minutes,
            expires_at=booking.expires_at,
            created_at=booking.created_at,
            image_id=booking.image_id,
            image_name=booking.image_name,
            hw_config_id=booking.hw_config_id,
            hw_config_name=booking.hw_config_name,
            resource_type=booking.resource_type.value,
            namespace_id=booking.namespace_id,
            static_vm_id=booking.static_vm_id,
            cpus=booking.cpus,
            memory_mb=booking.memory_mb,
            disk_mb=booking.disk_mb,
            drive_type=booking.drive_type,
            startup_script=booking.startup_script,
            config_roles=booking.config_roles or [],
            extra_vars=booking.extra_vars or {},
            environment_id=booking.environment_id,
            environment_label=booking.environment_label,
            label=booking.label,
            created_by=booking.created_by,
        )
        session.add(model)
        await session.flush()  # INSERT booking before audit to satisfy FK constraint
        session.add(BookingAuditModel(
            booking_id=booking.id,
            actor_id=booking.user_id,
            action="CREATED",
        ))
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def get(self, session: AsyncSession, booking_id: UUID) -> Booking:
        result = await session.execute(
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == BookingModel.created_by)
            .where(BookingModel.id == booking_id)
        )
        row = result.first()
        if row is None:
            raise BookingNotFoundError(booking_id)
        model, owner, namespace, static_vm, creator = row
        return _to_entity(model, owner_username=owner, namespace=namespace, static_vm=static_vm,
                          created_by_username=creator)

    async def update_status(
        self,
        session: AsyncSession,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
        vm_password: str | None = None,
        actor_id: str = "system",
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_status = model.status
        _check_transition(old_status, status, booking_id)
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        if vm_password is not None:
            model.vm_password = vm_password
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
        await session.commit()
        await _apublish_lifecycle(session, model)

    async def list_all(
        self,
        session: AsyncSession,
        include_released: bool = False,
        resource_type: str | list[str] | None = None,
        label: str | None = None,
    ) -> list[Booking]:
        stmt = (
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == BookingModel.created_by)
            .order_by(BookingModel.created_at.desc())
        )
        if not include_released:
            stmt = stmt.where(BookingModel.status != BookingStatus.RELEASED.value)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        stmt = _apply_label_filter(stmt, label)
        result = await session.execute(stmt)
        return [_to_entity(m, username, ns, svm, created_by_username=creator)
                for m, username, ns, svm, creator in result.all()]

    async def list_by_user(
        self,
        session: AsyncSession,
        user_id: str,
        include_released: bool = False,
        resource_type: str | list[str] | None = None,
        label: str | None = None,
    ) -> list[Booking]:
        # "Visible to user": bookings they own, plus any they dispatched on someone's behalf
        # (created_by). created_by is only ever a dispatcher/admin id, so for an ordinary user
        # this is just their own bookings.
        stmt = (
            select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel, _CreatorUser.username)
            .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
            .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
            .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
            .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == BookingModel.created_by)
            .where(or_(BookingModel.user_id == user_id, BookingModel.created_by == user_id))
            .order_by(BookingModel.created_at.desc())
        )
        if not include_released:
            stmt = stmt.where(BookingModel.status != BookingStatus.RELEASED.value)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        stmt = _apply_label_filter(stmt, label)
        result = await session.execute(stmt)
        return [_to_entity(m, username, ns, svm, created_by_username=creator)
                for m, username, ns, svm, creator in result.all()]

    async def list_audit(self, session: AsyncSession, booking_id: UUID) -> list[BookingAuditEntry]:
        result = await session.execute(
            select(BookingAuditModel)
            .where(BookingAuditModel.booking_id == booking_id)
            .order_by(BookingAuditModel.created_at)
        )
        return [_to_audit_entity(m) for m in result.scalars().all()]

    async def extend(
        self,
        session: AsyncSession,
        booking_id: UUID,
        extend_minutes: int,
        actor_id: str,
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        extended = Lease(model.ttl_minutes, model.expires_at).extended_by(extend_minutes)
        model.ttl_minutes = extended.ttl_minutes
        model.expires_at = extended.expires_at
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="EXTENDED",
            extra={"extend_minutes": extend_minutes},
        ))
        await session.commit()
        await _apublish_lifecycle(session, model)

    async def update_label(
        self, session: AsyncSession, booking_id: UUID, label: str | None, actor_id: str,
    ) -> None:
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_label = model.label
        model.label = label
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="LABEL_CHANGED",
            extra={"old_label": old_label, "new_label": label},
        ))
        await session.commit()
        await _apublish_lifecycle(session, model)

    async def get_live_standalone_namespace_booking(
        self, session: AsyncSession, user_id: str, namespace_id: UUID
    ) -> Booking | None:
        """Return the live, standalone booking (environment_id IS NULL) holding namespace_id for
        user_id, or None if no such booking exists."""
        result = await session.execute(
            select(BookingModel)
            .where(
                BookingModel.namespace_id == namespace_id,
                cast(BookingModel.user_id, String) == user_id,
                BookingModel.status.in_(_POOLED_LIVE_STATUSES),
                BookingModel.environment_id.is_(None),
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def set_environment(
        self,
        session: AsyncSession,
        booking_id: UUID,
        environment_id: UUID | None,
        environment_label: str | None,
        ttl_minutes: int,
        expires_at,
    ) -> None:
        """Re-point a booking's environment membership and lease fields.

        Called with a live env_id to adopt a standalone booking into an environment, or with
        environment_id=None and original ttl/expires_at to detach it on rollback.
        """
        result = await session.execute(select(BookingModel).where(BookingModel.id == booking_id))
        model = result.scalar_one_or_none()
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.environment_id = environment_id
        model.environment_label = environment_label
        model.ttl_minutes = ttl_minutes
        model.expires_at = expires_at
        action = "ADOPTED" if environment_id is not None else "DETACHED"
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id="system",
            action=action,
        ))
        await session.commit()

    async def promote_next_queued(self, session: AsyncSession, resource_type: str) -> Booking | None:
        """Assign the next free resource to the oldest QUEUED booking of this type → READY."""
        booking = (await session.execute(_oldest_queued_stmt(resource_type))).scalar_one_or_none()
        if booking is None:
            return None
        resource = (await session.execute(_free_resource_stmt(resource_type))).scalar_one_or_none()
        if resource is None:
            return None  # nothing free yet — stays queued
        _assign_resource_and_ready(session, booking, resource_type, resource)
        await session.commit()
        await session.refresh(booking)
        await _apublish_lifecycle(session, booking)
        if booking.environment_id is not None:
            # Only after the promotion commits, in its own transaction (lock order, #434 D3).
            await _environment_repo().start_lease_if_ready(session, booking.environment_id)
        return _to_entity(booking)

    async def queue_position(self, session: AsyncSession, resource_type: str, created_at: datetime) -> int:
        result = await session.execute(_queue_rank_stmt(resource_type, created_at))
        return result.scalar_one() + 1

    # Sync variants used by Celery workers
    def sync_get(self, session: Session, booking_id: UUID) -> Booking:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        return _to_entity(model)

    def sync_update_status(
        self,
        session: Session,
        booking_id: UUID,
        status: BookingStatus,
        vm_ip: str | None = None,
        vm_password: str | None = None,
        config_failed: bool | None = None,
        start_lease: bool = False,
        actor_id: str = "system",
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        old_status = model.status
        _check_transition(old_status, status, booking_id)
        model.status = status.value
        if vm_ip is not None:
            model.vm_ip = vm_ip
        if vm_password is not None:
            model.vm_password = vm_password
        if config_failed is not None:
            model.config_failed = config_failed
        if start_lease and status == BookingStatus.READY:
            # The lease grants usable time: start the TTL clock now that the VM is ready (#223),
            # rather than at creation when provisioning/configuration time would be deducted.
            model.expires_at = Lease.starting_now(model.ttl_minutes).expires_at
        session.add(BookingAuditModel(
            booking_id=booking_id,
            actor_id=actor_id,
            action="STATUS_CHANGED",
            old_status=old_status,
            new_status=status.value,
            extra={"vm_ip": vm_ip} if vm_ip is not None else None,
        ))
        session.commit()
        _publish_lifecycle(session, model)

    def sync_set_status_message(
        self, session: Session, booking_id: UUID, message: str | None
    ) -> None:
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.status_message = message
        session.commit()
        _publish_lifecycle(session, model)

    def sync_record_progress(self, session: Session, booking_id: UUID, message: str) -> None:
        """Set the compact status_message (unchanged) and append to the capped provisioning_log,
        in one commit rather than two — this fires on every Ansible/script output line (#378).

        Every line is committed, but its row-changed notification is coalesced per booking (#440).
        """
        model = session.get(BookingModel, booking_id)
        if model is None:
            raise BookingNotFoundError(booking_id)
        model.status_message = message
        combined = (model.provisioning_log or "") + message + "\n"
        model.provisioning_log = combined[-50_000:]
        session.commit()
        # No environment routing: a progress notification never refreshes the environment row.
        publish_progress_changed(
            booking_id=booking_id, booking_routing=_routing(model),
            environment_id=getattr(model, "environment_id", None),
        )

    def sync_list_expired(self, session: Session) -> list[Booking]:
        """Return READY standalone bookings whose expires_at is in the past.

        Environment children (``environment_id`` set) are excluded — they're released as a group by
        ``enforce_environment_ttl``, not individually.
        """
        result = session.execute(
            select(BookingModel).where(
                BookingModel.status == BookingStatus.READY.value,
                BookingModel.expires_at < datetime.now(timezone.utc),
                BookingModel.environment_id.is_(None),
            )
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_list_stale_provisioning(
        self, session: Session, threshold_minutes: int = 60
    ) -> list[Booking]:
        """Return PENDING/PROVISIONING/CONFIGURING/RETRY bookings older than threshold_minutes."""
        stale_statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
            BookingStatus.CONFIGURING.value,
            BookingStatus.RETRY.value,
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
        result = session.execute(
            select(BookingModel).where(
                BookingModel.status.in_(stale_statuses),
                BookingModel.created_at < cutoff,
            )
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_list_in_progress(self, session: Session) -> list[Booking]:
        """Return all PENDING/PROVISIONING/CONFIGURING/RETRY bookings regardless of age."""
        statuses = [
            BookingStatus.PENDING.value,
            BookingStatus.PROVISIONING.value,
            BookingStatus.CONFIGURING.value,
            BookingStatus.RETRY.value,
        ]
        result = session.execute(
            select(BookingModel).where(BookingModel.status.in_(statuses))
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_list_stuck_releasing(self, session: Session) -> list[Booking]:
        """Return all RELEASING bookings regardless of age (#374 — a killed teardown task
        never re-dispatches on its own; startup recovery re-attempts it)."""
        result = session.execute(
            select(BookingModel).where(BookingModel.status == BookingStatus.RELEASING.value)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    def sync_promote_next_queued(self, session: Session, resource_type: str) -> Booking | None:
        """Sync twin of promote_next_queued — called from the Celery teardown/TTL path."""
        booking = session.execute(_oldest_queued_stmt(resource_type)).scalar_one_or_none()
        if booking is None:
            return None
        resource = session.execute(_free_resource_stmt(resource_type)).scalar_one_or_none()
        if resource is None:
            return None
        _assign_resource_and_ready(session, booking, resource_type, resource)
        session.commit()
        _publish_lifecycle(session, booking)
        if booking.environment_id is not None:
            # Only after the promotion commits, in its own transaction (lock order, #434 D3).
            _environment_repo().sync_start_lease_if_ready(session, booking.environment_id)
        return _to_entity(booking)
