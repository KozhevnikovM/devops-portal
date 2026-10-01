import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, func, literal_column, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement


class Base(DeclarativeBase):
    pass


class VMImageModel(Base):
    __tablename__ = "vm_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    vapp_template_id: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HWConfigModel(Base):
    __tablename__ = "hw_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    cpus: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drive_type: Mapped[str] = mapped_column(String(8), nullable=False, default="HDD", server_default="HDD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NamespaceModel(Base):
    __tablename__ = "namespaces"
    # A namespace name is unique per-cluster, so the (name, cluster) pair identifies it.
    __table_args__ = (
        UniqueConstraint("name", "cluster_name", name="uq_namespaces_name_cluster"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    cluster_name: Mapped[str] = mapped_column(String(64), nullable=False)
    api_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RoleModel(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ansible_role: Mapped[str] = mapped_column(String(128), nullable=False)
    default_vars: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    secret_vars: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EnvironmentBlueprintModel(Base):
    __tablename__ = "environment_blueprints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    items: Mapped[list["EnvironmentBlueprintItemModel"]] = relationship(
        back_populates="blueprint", cascade="all, delete-orphan",
        order_by="EnvironmentBlueprintItemModel.position",
    )


class EnvironmentBlueprintItemModel(Base):
    __tablename__ = "environment_blueprint_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    blueprint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environment_blueprints.id", ondelete="CASCADE"), nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    blueprint: Mapped["EnvironmentBlueprintModel"] = relationship(back_populates="items")


class EnvironmentModel(Base):
    __tablename__ = "environments"
    __table_args__ = (
        # Keyset pagination of the environments page walks this backward (#467).
        Index("ix_environments_created_at_id", "created_at", "id"),
        # Mine is two keyset walks, owned and dispatched, each in page order on its own index, so
        # its read is bounded by the viewer's own history rather than everyone's (#496). The
        # creator index is partial: most environments are not dispatched, and `created_by = :me`
        # implies the predicate.
        Index("ix_environments_owner_page", "user_id", "created_at", "id"),
        Index(
            "ix_environments_creator_page", "created_by", "created_at", "id",
            postgresql_where=text("created_by IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    blueprint_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # False while the order is still creating children; the lease can't start until it's True (#434).
    construction_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )


class StaticVMModel(Base):
    __tablename__ = "static_vms"
    __table_args__ = (
        CheckConstraint(
            "password IS NOT NULL OR ssh_key IS NOT NULL",
            name="ck_static_vms_credential_present",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ssh_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# Status predicates of the bookings partial indexes. Queries must spell them as literals too
# (BOOKING_NOT_RELEASED / BOOKING_QUEUED below): the planner uses a partial index only when it can
# prove the query's predicate implies the index's, which a bound parameter under a generic plan
# can't (#479). tests/test_booking_pagination.py pins the two spellings together.
BOOKING_NOT_RELEASED_SQL = "status <> 'RELEASED'"
BOOKING_QUEUED_SQL = "status = 'QUEUED'"


class BookingModel(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # Child lookups by environment, and the "has a non-RELEASED child" probe of the
        # environments list (#466). Kept in step with migration 0033. The bookings-page indexes
        # (#479) are declared below the class, from the page-key expressions the queries use.
        Index("ix_bookings_environment_id", "environment_id"),
        Index(
            "ix_bookings_environment_id_unreleased", "environment_id",
            postgresql_where=text(BOOKING_NOT_RELEASED_SQL),
        ),
        # FIFO queue rank: reads only the queue, never history (#479).
        Index(
            "ix_bookings_queued_rank", "resource_type", "created_at",
            postgresql_where=text(BOOKING_QUEUED_SQL),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False, default="VM", server_default="VM")
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    image_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vm_images.id"), nullable=True)
    image_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hw_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("hw_configs.id"), nullable=True)
    hw_config_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    namespace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("namespaces.id"), nullable=True)
    static_vm_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("static_vms.id"), nullable=True)
    cpus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disk_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drive_type: Mapped[str] = mapped_column(String(8), nullable=False, default="HDD", server_default="HDD")
    vm_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vm_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    provisioning_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    startup_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    extra_vars: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    config_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id", ondelete="SET NULL"), nullable=True,
    )
    environment_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    vms: Mapped[list["VMModel"]] = relationship("VMModel", back_populates="booking", cascade="all, delete-orphan")


# The partial-index predicates above as column-qualified SQL literals, for queries (#479).
BOOKING_NOT_RELEASED = BookingModel.status != literal_column("'RELEASED'")
BOOKING_QUEUED = BookingModel.status == literal_column("'QUEUED'")
BOOKING_HAS_CREATOR = BookingModel.created_by.is_not(None)


# Bookings-page keyset walks (#479, design.md Decisions 2 and 10). Every walk is one branch: a
# page scope (All by type, Mine by owner, Mine by creator) × released hidden or shown. Each branch
# has its own index, led by its own page-key expression — e.g. 'ol:' || user_id || ':' ||
# resource_type for "owner, RELEASED rows excluded" — followed by the (created_at, id) page order.
# A branch's query constrains exactly that expression, so its index is the only one with usable
# conditions: a broader ordered index (the type index for a Mine branch, the full index for a
# hidden-released branch) can't serve it, and the walk can't stray into history it doesn't match.
_PAGE_SCOPES = {
    # scope: (key tag, owner column or None, extra partial predicate or None)
    "type": ("t", None, None),
    "owner": ("o", BookingModel.user_id, None),
    "creator": ("c", BookingModel.created_by, "created_by IS NOT NULL"),
}


def booking_page_key(scope: str, unreleased: bool):
    """The page-key expression of a branch's index. Literal separators, never bind parameters,
    so the query's expression is the index's expression under any (generic) plan."""
    tag, owner, _ = _PAGE_SCOPES[scope]
    expr: ColumnElement[str] = literal_column(f"'{tag}{'l' if unreleased else ''}:'", String)
    if owner is not None:
        expr = expr + owner + literal_column("':'", String)
    return expr + BookingModel.resource_type


def booking_page_key_value(scope: str, unreleased: bool, resource_type: str, owner: str | None) -> str:
    """The value `booking_page_key(scope, unreleased)` has for a booking of this type/owner."""
    tag, owner_column, _ = _PAGE_SCOPES[scope]
    prefix = f"{tag}{'l' if unreleased else ''}:"
    return f"{prefix}{owner}:{resource_type}" if owner_column is not None else f"{prefix}{resource_type}"


def _page_index_where(scope: str, unreleased: bool) -> str | None:
    predicates = [p for p in (_PAGE_SCOPES[scope][2], BOOKING_NOT_RELEASED_SQL if unreleased else None) if p]
    return " AND ".join(predicates) or None


BOOKING_PAGE_INDEXES = {
    (scope, unreleased): Index(
        f"ix_bookings_{scope}_page{'_unreleased' if unreleased else ''}",
        booking_page_key(scope, unreleased), BookingModel.created_at, BookingModel.id,
        postgresql_where=text(where) if (where := _page_index_where(scope, unreleased)) else None,
    )
    for scope in _PAGE_SCOPES
    for unreleased in (False, True)
}


class VMModel(Base):
    __tablename__ = "vms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking: Mapped["BookingModel"] = relationship("BookingModel", back_populates="vms")


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC", server_default="UTC")
    default_image_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vm_images.id"), nullable=True)
    default_hw_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("hw_configs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    api_keys: Mapped[list["APIKeyModel"]] = relationship("APIKeyModel", back_populates="user", cascade="all, delete-orphan")


class APIKeyModel(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    description: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="api_keys")


class QuotaModel(Base):
    __tablename__ = "quotas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    max_cpus: Mapped[int] = mapped_column(Integer, nullable=False)
    max_memory_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_ssd_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hdd_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BookingAuditModel(Base):
    __tablename__ = "booking_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
