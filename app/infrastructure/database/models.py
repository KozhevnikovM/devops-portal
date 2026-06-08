import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class BookingModel(Base):
    __tablename__ = "bookings"

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
    startup_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    config_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    vms: Mapped[list["VMModel"]] = relationship("VMModel", back_populates="booking", cascade="all, delete-orphan")


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
