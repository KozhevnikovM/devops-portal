"""vm quota: split hw_config disk, booking resource snapshot, quotas table

Revision ID: 0007
Revises: 0005
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. hw_configs: add ssd_mb / hdd_mb, migrate disk_mb → hdd_mb, drop disk_mb ──
    op.add_column("hw_configs", sa.Column("ssd_mb", sa.Integer, nullable=False, server_default="0"))
    op.add_column("hw_configs", sa.Column("hdd_mb", sa.Integer, nullable=False, server_default="0"))
    op.execute("UPDATE hw_configs SET hdd_mb = disk_mb")
    op.drop_column("hw_configs", "disk_mb")

    # ── 2. bookings: add resource snapshot columns (backfill from hw_configs) ──
    op.add_column("bookings", sa.Column("cpus",      sa.Integer, nullable=False, server_default="0"))
    op.add_column("bookings", sa.Column("memory_mb", sa.Integer, nullable=False, server_default="0"))
    op.add_column("bookings", sa.Column("ssd_mb",    sa.Integer, nullable=False, server_default="0"))
    op.add_column("bookings", sa.Column("hdd_mb",    sa.Integer, nullable=False, server_default="0"))
    op.execute("""
        UPDATE bookings b
        SET cpus      = h.cpus,
            memory_mb = h.memory_mb,
            ssd_mb    = 0,
            hdd_mb    = h.hdd_mb
        FROM hw_configs h
        WHERE b.hw_config_id = h.id
    """)

    # ── 3. quotas table ──
    op.create_table(
        "quotas",
        sa.Column("id",            UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id",       UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("max_cpus",      sa.Integer, nullable=False),
        sa.Column("max_memory_gb", sa.Integer, nullable=False),
        sa.Column("max_hdd_gb",    sa.Integer, nullable=False),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("quotas")

    op.drop_column("bookings", "hdd_mb")
    op.drop_column("bookings", "ssd_mb")
    op.drop_column("bookings", "memory_mb")
    op.drop_column("bookings", "cpus")

    op.add_column("hw_configs", sa.Column("disk_mb", sa.Integer, nullable=False, server_default="0"))
    op.execute("UPDATE hw_configs SET disk_mb = hdd_mb")
    op.drop_column("hw_configs", "hdd_mb")
    op.drop_column("hw_configs", "ssd_mb")
