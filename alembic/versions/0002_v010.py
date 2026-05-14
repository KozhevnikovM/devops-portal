"""vm template catalog

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

_UBUNTU_2204_ID = "a1111111-0000-0000-0000-000000000001"
_UBUNTU_2004_ID = "a1111111-0000-0000-0000-000000000002"
_WIN2022_ID     = "a1111111-0000-0000-0000-000000000003"


def upgrade() -> None:
    op.create_table(
        "vm_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("vapp_template_id", sa.String(256), nullable=False),
        sa.Column("cpus", sa.Integer, nullable=False),
        sa.Column("memory_mb", sa.Integer, nullable=False),
        sa.Column("disk_mb", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.execute(f"""
        INSERT INTO vm_templates (id, name, vapp_template_id, cpus, memory_mb, disk_mb) VALUES
        ('{_UBUNTU_2204_ID}', 'Ubuntu 22.04', 'changeme-ubuntu-2204', 2, 4096, 26624),
        ('{_UBUNTU_2004_ID}', 'Ubuntu 20.04', 'changeme-ubuntu-2004', 2, 4096, 26624),
        ('{_WIN2022_ID}',     'Windows 2022', 'changeme-win2022',     4, 8192, 51200)
    """)

    op.add_column("bookings", sa.Column("template_id", UUID(as_uuid=True), nullable=True))
    op.add_column("bookings", sa.Column("template_name", sa.String(64), nullable=True))

    op.execute(f"""
        UPDATE bookings
        SET template_id   = '{_UBUNTU_2204_ID}',
            template_name = 'Ubuntu 22.04'
        WHERE template_id IS NULL
    """)

    op.alter_column("bookings", "template_id", nullable=False)
    op.alter_column("bookings", "template_name", nullable=False)

    op.create_foreign_key(
        "fk_bookings_template_id",
        "bookings", "vm_templates",
        ["template_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_template_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "template_name")
    op.drop_column("bookings", "template_id")
    op.drop_table("vm_templates")
