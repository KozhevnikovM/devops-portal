import logging
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import (
    CursorResult, case, cast, column, Connection, Engine, false, func, literal, or_, select, String, Text, true,
    tuple_, union_all, update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, aliased

from app.domain.booking_list import BookingListItem
from app.domain.booking_status import LIVE_STATUSES, can_transition
from app.domain.constants import PROVISIONING_LOG_MAX_CHARS
from app.domain.entities import Booking, BookingAuditEntry
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import BookingNotFoundError, IllegalStatusTransitionError
from app.domain.lease import Lease
from app.domain.pagination import KeysetCursor, KeysetPage
from app.domain.resource_details import (
    NamespaceDetails, ResourceFootprint, StaticVMDetails, VMDetails,
)
from app.infrastructure.database.models import (
    BOOKING_HAS_CREATOR, BOOKING_NOT_RELEASED, BOOKING_QUEUED, BookingAuditModel, BookingModel,
    EnvironmentModel, NamespaceModel, StaticVMModel, UserModel, booking_page_key,
    booking_page_key_value,
)
from app.infrastructure.events import (
    Routing,
    apublish_row_changed,
    publish_progress_changed,
    publish_row_changed,
)
from app.infrastructure.repositories._ordered_walk import _OrderedWalk

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


def _role_names_expr():
    """Configured role names in array order, as text[] — the roles' vars/secret_vars stay in the
    database. ``ARRAY(SELECT ...)`` yields ``{}`` for an empty role list, never NULL."""
    role = func.jsonb_array_elements(BookingModel.config_roles).table_valued(
        column("value", JSONB), with_ordinality="ord",
    ).render_derived(name="cfg_role")
    names = select(role.c.value["name"].astext).order_by(role.c.ord).scalar_subquery()
    return func.array(names, type_=ARRAY(Text))


def _non_empty(col):
    """``col`` is non-NULL and non-empty — Python truthiness, without returning the value."""
    return func.coalesce(func.octet_length(col), 0) > 0


def _has_credentials_expr():
    """Same rule as ``Booking.has_credentials``: no per-type branch is needed, since a VM has no
    static-VM join, a static VM no VM password, and a namespace neither."""
    return or_(
        _non_empty(BookingModel.vm_password),
        _non_empty(StaticVMModel.username),
        _non_empty(StaticVMModel.password),
        _non_empty(StaticVMModel.ssh_key),
    )


def _list_item_stmt():
    """Bulk booking-list read (#477): exactly the BookingListItem columns, labelled by field name.

    Never selects the BookingModel entity, nor provisioning_log / startup_script / extra_vars /
    config_roles — the log is reduced to a presence flag (``octet_length`` reads the TOAST header,
    not the value) and the roles to their names. Credential values (VM password, static-VM
    password / SSH key) never leave the database either (#478): they are reduced to
    ``has_credentials``, and revealed per booking by ``GET /bookings/{id}/credentials``.
    """
    return (
        select(
            BookingModel.id.label("id"),
            BookingModel.user_id.label("user_id"),
            BookingModel.status.label("status"),
            BookingModel.resource_type.label("resource_type"),
            BookingModel.ttl_minutes.label("ttl_minutes"),
            BookingModel.expires_at.label("expires_at"),
            BookingModel.created_at.label("created_at"),
            BookingModel.label.label("label"),
            BookingModel.status_message.label("status_message"),
            BookingModel.config_failed.label("config_failed"),
            BookingModel.environment_id.label("environment_id"),
            UserModel.username.label("owner_username"),
            BookingModel.created_by.label("created_by"),
            _CreatorUser.username.label("created_by_username"),
            BookingModel.image_id.label("image_id"),
            BookingModel.image_name.label("image_name"),
            BookingModel.hw_config_id.label("hw_config_id"),
            BookingModel.hw_config_name.label("hw_config_name"),
            BookingModel.vm_ip.label("vm_ip"),
            NamespaceModel.name.label("namespace_name"),
            NamespaceModel.cluster_name.label("cluster_name"),
            NamespaceModel.api_url.label("api_url"),
            StaticVMModel.name.label("static_vm_name"),
            StaticVMModel.host.label("static_vm_host"),
            StaticVMModel.username.label("static_vm_username"),
            _non_empty(BookingModel.provisioning_log).label("has_provisioning_log"),
            _role_names_expr().label("config_role_names"),
            _has_credentials_expr().label("has_credentials"),
        )
        .select_from(BookingModel)
        .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
        .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
        .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
        .outerjoin(_CreatorUser, cast(_CreatorUser.id, String) == BookingModel.created_by)
        # id breaks created_at ties (one transaction's bookings share a timestamp), so the order
        # is total — keyset pagination relies on it (#479).
        .order_by(BookingModel.created_at.desc(), BookingModel.id.desc())
    )


def _list_items_by_ids_stmt(
    ids: Sequence[UUID], *, user_id: str | None = None, resource_types: Sequence[str] | None = None,
):
    """The list projection of the given bookings, in page order — the hydration phase of a page
    (#479) and the batch read of page reconciliation (#497).

    Reconciliation passes the page's scope: `resource_types` keeps only the page's kinds, and
    `user_id` the Mine rule. An id outside that scope simply doesn't come back, so authorization
    is the query itself. Neither Show released nor the label filter applies: a displayed row that
    was released or relabelled still has to be refreshed in place.
    """
    stmt = _list_item_stmt().where(BookingModel.id.in_(list(ids)))
    if resource_types is not None:
        stmt = stmt.where(BookingModel.resource_type.in_(list(resource_types)))
    if user_id is not None:
        stmt = stmt.where(_owner_filter(user_id))
    return stmt


def _to_list_item(row) -> BookingListItem:
    fields = dict(row._mapping)
    try:
        fields["status"] = BookingStatus(fields["status"])
    except ValueError:
        raise ValueError(
            f"Booking {fields['id']} has unrecognised status {fields['status']!r} in the database"
        )
    fields["resource_type"] = ResourceType(fields["resource_type"])
    fields["config_role_names"] = tuple(fields["config_role_names"] or ())
    return BookingListItem(**fields)


def _apply_resource_type_filter(stmt, resource_type: str | list[str] | None):
    """Filter by a single resource_type or any of a list (VM page wants VM + STATIC_VM)."""
    if resource_type is None:
        return stmt
    if isinstance(resource_type, (list, tuple, set)):
        return stmt.where(BookingModel.resource_type.in_(list(resource_type)))
    return stmt.where(BookingModel.resource_type == resource_type)


def _label_filter_text(label: str | None) -> str | None:
    """The label filter's search text, or None when no label filter applies (blank/None)."""
    return (label or "").strip() or None


def _apply_label_filter(stmt, label: str | None):
    """Case-insensitive substring match on the booking's label; no-op if blank/None."""
    text_ = _label_filter_text(label)
    if text_ is None:
        return stmt
    return stmt.where(BookingModel.label.ilike(f"%{text_}%"))


def _owner_filter(user_id: str):
    """"Visible to user" for the Mine list: bookings they own, plus any they dispatched on someone's
    behalf (created_by). created_by is only ever a dispatcher/admin id, so for an ordinary user
    this is just their own bookings."""
    return or_(BookingModel.user_id == user_id, BookingModel.created_by == user_id)


def _apply_released_filter(stmt, include_released: bool):
    """Hide RELEASED bookings unless shown — as a literal, so the partial indexes match (#479)."""
    return stmt if include_released else stmt.where(BOOKING_NOT_RELEASED)


def _page_window_stmt(
    user_id: str | None, *, resource_types: list[str], include_released: bool, size: int,
    after: KeysetCursor | None,
):
    """The (created_at, id) keys of the first `size` bookings after `after`, in page order, of the
    page's owner / type / released range — no label test (#479, #485).

    `OR` across owner columns and `IN` across resource types can't be read in page order from
    one index, so this runs one ordered keyset walk per page scope (Mine: owner, creator; All:
    type) and resource type, then merges them. Each branch constrains exactly its own page-key
    expression (`booking_page_key`), so its own ix_bookings_*_page* index is the only one it can
    use: a walk that stops after `size` entries, all of which match. The top `size` of the union
    is the top `size` of the branches' tops, so at most 4 × size entries are read and sorted.
    GROUP BY drops a booking that both Mine branches found (dispatched to oneself).
    """
    unreleased = not include_released
    scopes = ["owner", "creator"] if user_id is not None else ["type"]
    branches = []
    for scope in scopes:
        for resource_type in resource_types:
            branch = select(BookingModel.created_at, BookingModel.id).where(
                booking_page_key(scope, unreleased)
                == booking_page_key_value(scope, unreleased, resource_type, user_id)
            )
            if scope == "creator":
                branch = branch.where(BOOKING_HAS_CREATOR)   # implies the creator index predicate
            branch = _apply_released_filter(branch, include_released)
            if after is not None:
                # A row comparison is an index condition on (created_at, id) after the page key,
                # so each walk starts at the cursor. Typed binds, as in environment_repo.
                branch = branch.where(
                    tuple_(BookingModel.created_at, BookingModel.id)
                    < tuple_(
                        literal(after.created_at, BookingModel.created_at.type),
                        literal(after.id, BookingModel.id.type),
                    )
                )
            branches.append(
                branch.order_by(BookingModel.created_at.desc(), BookingModel.id.desc()).limit(size)
            )
    keys = union_all(*branches).subquery("page_keys")
    return (
        select(keys.c.created_at, keys.c.id)
        .group_by(keys.c.created_at, keys.c.id)
        .order_by(keys.c.created_at.desc(), keys.c.id.desc())
        .limit(size)
    )


def _page_keys_stmt(
    user_id: str | None, *, resource_types: list[str], include_released: bool, limit: int,
    after: KeysetCursor | None,
):
    """Phase 1 of an unlabelled bookings page (#479): the keys of up to `limit + 1` bookings —
    every one a match, the extra one only telling whether another page exists."""
    return _page_window_stmt(
        user_id, resource_types=resource_types, include_released=include_released,
        size=limit + 1, after=after,
    )


def _label_page_keys_stmt(
    user_id: str | None, *, resource_types: list[str], label: str, include_released: bool,
    limit: int, scan_size: int, after: KeysetCursor | None,
):
    """Phase 1 of a label-filtered bookings page (#485, design.md Decision 2).

    No btree serves a substring match in page order, so instead of walking until `limit + 1`
    labels match (which on a sparse label walks the whole range), this examines a fixed window:
    the first `scan_size` bookings of the page's range, via the same page-key walks as an
    unlabelled page, and tests labels only inside it. One more key-only "probe" entry tells
    whether anything older than the window exists, without testing its label.

    Rows: up to `limit + 1` matches (`is_window_end` false) in page order, then — only when the
    probe exists — one sentinel (`is_window_end` true) holding the oldest examined key. The
    window CTEs are MATERIALIZED so the label test can't be pushed down into the walks, which
    would turn them back into filtered walks.
    """
    window = _page_window_stmt(
        user_id, resource_types=resource_types, include_released=include_released,
        size=scan_size + 1, after=after,
    ).cte("page_window").prefix_with("MATERIALIZED")
    examined = (
        select(window.c.created_at, window.c.id)
        .order_by(window.c.created_at.desc(), window.c.id.desc())
        .limit(scan_size)
        .cte("page_examined")
        .prefix_with("MATERIALIZED")
    )
    matches = (
        _apply_label_filter(
            select(examined.c.created_at, examined.c.id, false().label("is_window_end"))
            .join(BookingModel, BookingModel.id == examined.c.id),
            label,
        )
        .order_by(examined.c.created_at.desc(), examined.c.id.desc())
        .limit(limit + 1)
        .subquery("page_matches")
    )
    oldest = (
        select(examined.c.created_at, examined.c.id)
        .order_by(examined.c.created_at, examined.c.id)
        .limit(1)
        .subquery("page_oldest")
    )
    window_end = select(oldest.c.created_at, oldest.c.id, true().label("is_window_end")).where(
        select(func.count()).select_from(window).scalar_subquery() > scan_size
    )
    rows = union_all(
        select(matches.c.created_at, matches.c.id, matches.c.is_window_end), window_end,
    ).subquery("page_rows")
    return select(rows.c.created_at, rows.c.id, rows.c.is_window_end).order_by(
        rows.c.is_window_end, rows.c.created_at.desc(), rows.c.id.desc(),
    )


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


def _queue_rank_stmt(newest_by_type: dict[str, datetime], booking_ids: list[UUID]):
    """FIFO positions of `booking_ids` in one statement (#495).

    One branch per resource type walks that type's queue in ix_bookings_queued_rank order (the
    literal QUEUED predicate lets the partial index serve it, so history is never read, #479) up
    to the newest requested booking of the type — each entry once, however many are requested.
    rank() is 1 + the number of strictly earlier entries, so tied created_at share a position;
    the id filter sits above the window, so it cannot change any rank.
    """
    branches = [
        select(
            BookingModel.id.label("id"),
            func.rank().over(order_by=BookingModel.created_at).label("position"),
        ).where(
            BookingModel.resource_type == resource_type,
            BOOKING_QUEUED,
            BookingModel.created_at <= newest,
        )
        for resource_type, newest in newest_by_type.items()
    ]
    ranked = (branches[0] if len(branches) == 1 else union_all(*branches)).subquery()
    return select(ranked.c.id, ranked.c.position).where(ranked.c.id.in_(booking_ids))


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
    ) -> list[BookingListItem]:
        stmt = _apply_released_filter(_list_item_stmt(), include_released)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        stmt = _apply_label_filter(stmt, label)
        result = await session.execute(stmt)
        return [_to_list_item(row) for row in result.all()]

    async def list_by_user(
        self,
        session: AsyncSession,
        user_id: str,
        include_released: bool = False,
        resource_type: str | list[str] | None = None,
        label: str | None = None,
    ) -> list[BookingListItem]:
        stmt = _apply_released_filter(_list_item_stmt().where(_owner_filter(user_id)), include_released)
        stmt = _apply_resource_type_filter(stmt, resource_type)
        stmt = _apply_label_filter(stmt, label)
        result = await session.execute(stmt)
        return [_to_list_item(row) for row in result.all()]

    async def list_page(
        self,
        session: AsyncSession,
        *,
        user_id: str | None,
        resource_types: list[str],
        label: str | None,
        include_released: bool,
        limit: int,
        scan_size: int,
        after: KeysetCursor | None,
    ) -> KeysetPage[BookingListItem]:
        """One keyset page of a bookings list, newest first (#479).

        `user_id=None` lists everyone's bookings, otherwise the Mine list. The page starts strictly
        after `after`. Two phases: the bounded key query (pinned to ordered index walks), then the
        list projection for the kept ids alone.

        Without a label, one extra key is fetched only to tell whether another page exists. With a
        label (#485), at most `scan_size` bookings are examined, so the page may be short — even
        empty — while older matches exist; its cursor is then the last *examined* booking.
        """
        label = _label_filter_text(label)
        async with _OrderedWalk(session):
            if label is not None:
                rows = (await session.execute(_label_page_keys_stmt(
                    user_id, resource_types=resource_types, label=label,
                    include_released=include_released, limit=limit, scan_size=scan_size,
                    after=after,
                ))).all()
            else:
                rows = (await session.execute(_page_keys_stmt(
                    user_id, resource_types=resource_types,
                    include_released=include_released, limit=limit, after=after,
                ))).all()
        # Matches and the window-end sentinel are told apart by the flag alone — never by
        # position or key: the sentinel's key is also a match's when the oldest examined matched.
        keys = [r for r in rows if not getattr(r, "is_window_end", False)]
        window_end = next((r for r in rows if getattr(r, "is_window_end", False)), None)
        has_more = len(keys) > limit
        keys = keys[:limit]
        items: list[BookingListItem] = []
        if keys:
            result = await session.execute(_list_items_by_ids_stmt([k.id for k in keys]))
            items = [_to_list_item(row) for row in result.all()]
        next_cursor = None
        if has_more:                   # a full page: continue after its last booking
            last = keys[-1]
            next_cursor = KeysetCursor(created_at=last.created_at, id=last.id)
        elif window_end is not None:   # the label scan ran out, and older bookings exist
            next_cursor = KeysetCursor(created_at=window_end.created_at, id=window_end.id)
        return KeysetPage(items=items, next_cursor=next_cursor)

    async def list_items_by_ids(
        self, session: AsyncSession, ids: Sequence[UUID], *, user_id: str | None,
        resource_types: Sequence[str],
    ) -> list[BookingListItem]:
        """The list projection of those of `ids` visible on a page, in one statement (#497).

        `user_id=None` is the All scope, otherwise Mine. Ids outside the scope (or unknown) are
        absent from the result. No statement when `ids` is empty.
        """
        if not ids:
            return []
        result = await session.execute(
            _list_items_by_ids_stmt(ids, user_id=user_id, resource_types=resource_types)
        )
        return [_to_list_item(row) for row in result.all()]

    async def newest_key(
        self, session: AsyncSession, *, user_id: str | None, resource_types: list[str],
        label: str | None, include_released: bool, scan_size: int,
    ) -> KeysetCursor | None:
        """The key of a page's newest matching booking, or None (#497 D8).

        Exactly the first page's key walk with room for one row — the same scope, filters and
        label-scan bound — and nothing else: one statement under the ordered-walk pin, no
        hydration. Page reconciliation compares it with the newest displayed row.
        """
        label = _label_filter_text(label)
        async with _OrderedWalk(session):
            if label is not None:
                stmt = _label_page_keys_stmt(
                    user_id, resource_types=resource_types, label=label,
                    include_released=include_released, limit=0, scan_size=scan_size, after=None,
                )
            else:
                stmt = _page_keys_stmt(
                    user_id, resource_types=resource_types, include_released=include_released,
                    limit=0, after=None,
                )
            rows = (await session.execute(stmt)).all()
        match = next((r for r in rows if not getattr(r, "is_window_end", False)), None)
        return None if match is None else KeysetCursor(created_at=match.created_at, id=match.id)

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

    async def queue_positions(
        self, session: AsyncSession, bookings: Sequence[Booking | BookingListItem],
    ) -> dict[UUID, int]:
        """FIFO positions of the given (expected QUEUED) bookings, by id, in one statement.

        A booking no longer QUEUED when this runs has no entry. No statement when empty.
        Runs under the ordered-walk pin (#495 D5): when the queue is most of the table the
        planner would otherwise prefer a Seq Scan + Sort that reads non-queued rows too.
        """
        if not bookings:
            return {}
        newest_by_type: dict[str, datetime] = {}
        for b in bookings:
            key = b.resource_type.value
            newest_by_type[key] = max(b.created_at, newest_by_type.get(key, b.created_at))
        async with _OrderedWalk(session):
            result = await session.execute(
                _queue_rank_stmt(newest_by_type, [b.id for b in bookings])
            )
            return {row.id: row.position for row in result}

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
        self,
        session: Session,
        booking_id: UUID,
        message: str | None,
        if_status_in: frozenset[BookingStatus] | None = None,
    ) -> bool:
        """Set (or clear) the status message; return whether it was written.

        With ``if_status_in``, the write is conditional on the booking's status being one of those,
        evaluated atomically in the UPDATE itself (#444 review): the provisioning task uses it so a
        release that lands between its own read and write can't have teardown's message erased.
        """
        if if_status_in is None:
            model = session.get(BookingModel, booking_id)
            if model is None:
                raise BookingNotFoundError(booking_id)
            model.status_message = message
            session.commit()
            _publish_lifecycle(session, model)
            return True
        # An UPDATE returns a CursorResult (with rowcount), though typed as the generic Result.
        result = type_cast(CursorResult, session.execute(
            update(BookingModel)
            .where(BookingModel.id == booking_id, BookingModel.status.in_([s.value for s in if_status_in]))
            .values(status_message=message)
            .execution_options(synchronize_session=False)
        ))
        if result.rowcount == 0:
            if session.get(BookingModel, booking_id) is None:
                raise BookingNotFoundError(booking_id)
            return False  # status no longer owned by the caller — leave the message alone
        session.commit()
        model = session.get(BookingModel, booking_id)
        if model is not None:  # deleted in between: nothing left to announce
            _publish_lifecycle(session, model)
        return True

    def sync_append_progress(
        self,
        session: Session,
        booking_id: UUID,
        chunk: str,
        last_message: str,
        accepting: frozenset[BookingStatus],
    ) -> None:
        """Persist one batch of progress output in a single atomic UPDATE (#444).

        Appends ``chunk`` (already newline-terminated lines) to the capped provisioning_log and sets
        status_message to ``last_message`` — but only while the booking's status is in
        ``accepting``, so a late batch can never overwrite a lifecycle outcome (READY/FAILED/RETRY,
        a config-error message, or teardown's own progress after a release). Appending in SQL
        rather than load-modify-commit means overlapping producers can't lose each other's lines.

        One progress notification follows the commit; it is coalesced per booking (#440).
        """
        stmt = (
            update(BookingModel)
            .where(BookingModel.id == booking_id)
            .values(
                provisioning_log=func.right(
                    func.coalesce(BookingModel.provisioning_log, "") + chunk, PROVISIONING_LOG_MAX_CHARS,
                ),
                status_message=case(
                    (BookingModel.status.in_([s.value for s in accepting]), last_message),
                    else_=BookingModel.status_message,
                ),
            )
            .returning(BookingModel.user_id, BookingModel.created_by, BookingModel.environment_id)
            .execution_options(synchronize_session=False)
        )
        row = session.execute(stmt).one_or_none()
        if row is None:
            raise BookingNotFoundError(booking_id)
        session.commit()
        # No environment routing: a progress notification never refreshes the environment row.
        publish_progress_changed(
            booking_id=booking_id,
            booking_routing=Routing(owner_id=row.user_id, created_by=row.created_by),
            environment_id=row.environment_id,
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
