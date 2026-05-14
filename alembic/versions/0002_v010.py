"""vm image and hardware catalog

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_UBUNTU_2204_ID = "a1000000-0000-0000-0000-000000000001"
_UBUNTU_2004_ID = "a1000000-0000-0000-0000-000000000002"
_WIN2022_ID     = "a1000000-0000-0000-0000-000000000003"

_HW_SMALL_ID    = "b2000000-0000-0000-0000-000000000001"
_HW_MEDIUM_ID   = "b2000000-0000-0000-0000-000000000002"
_HW_LARGE_ID    = "b2000000-0000-0000-0000-000000000003"


def upgrade() -> None:
    op.create_table(
        "vm_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("vapp_template_id", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "hw_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("cpus", sa.Integer, nullable=False),
        sa.Column("memory_mb", sa.Integer, nullable=False),
        sa.Column("disk_mb", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.execute(f"""
        INSERT INTO vm_images (id, name, vapp_template_id) VALUES
        ('{_UBUNTU_2204_ID}', 'Ubuntu 22.04', 'changeme-ubuntu-2204'),
        ('{_UBUNTU_2004_ID}', 'Ubuntu 20.04', 'changeme-ubuntu-2004'),
        ('{_WIN2022_ID}',     'Windows 2022', 'changeme-win2022')
    """)

    op.execute(f"""
        INSERT INTO hw_configs (id, name, cpus, memory_mb, disk_mb) VALUES
        ('{_HW_SMALL_ID}',  'small',  1, 2048,  13312),
        ('{_HW_MEDIUM_ID}', 'medium', 2, 4096,  26624),
        ('{_HW_LARGE_ID}',  'large',  4, 8192,  51200)
    """)

    op.add_column("bookings", sa.Column("image_id", UUID(as_uuid=True), nullable=True))
    op.add_column("bookings", sa.Column("image_name", sa.String(64), nullable=True))
    op.add_column("bookings", sa.Column("hw_config_id", UUID(as_uuid=True), nullable=True))
    op.add_column("bookings", sa.Column("hw_config_name", sa.String(64), nullable=True))

    op.execute(f"""
        UPDATE bookings
        SET image_id      = '{_UBUNTU_2204_ID}',
            image_name    = 'Ubuntu 22.04',
            hw_config_id  = '{_HW_SMALL_ID}',
            hw_config_name = 'small'
        WHERE image_id IS NULL
    """)

    op.alter_column("bookings", "image_id", nullable=False)
    op.alter_column("bookings", "image_name", nullable=False)
    op.alter_column("bookings", "hw_config_id", nullable=False)
    op.alter_column("bookings", "hw_config_name", nullable=False)

    op.create_foreign_key("fk_bookings_image_id", "bookings", "vm_images", ["image_id"], ["id"])
    op.create_foreign_key("fk_bookings_hw_config_id", "bookings", "hw_configs", ["hw_config_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_bookings_hw_config_id", "bookings", type_="foreignkey")
    op.drop_constraint("fk_bookings_image_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "hw_config_name")
    op.drop_column("bookings", "hw_config_id")
    op.drop_column("bookings", "image_name")
    op.drop_column("bookings", "image_id")
    op.drop_table("hw_configs")
    op.drop_table("vm_images")
