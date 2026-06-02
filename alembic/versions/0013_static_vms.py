"""static VM inventory catalog + booking static_vm_id

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "static_vms",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("host", sa.String(256), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password", sa.String(256), nullable=True),
        sa.Column("ssh_key", sa.Text(), nullable=True),
        sa.Column("cpus", sa.Integer(), nullable=True),
        sa.Column("memory_mb", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # at least one credential (password or ssh_key) must be present
        sa.CheckConstraint(
            "password IS NOT NULL OR ssh_key IS NOT NULL",
            name="ck_static_vms_credential_present",
        ),
    )

    op.add_column("bookings", sa.Column("static_vm_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_static_vm_id", "bookings", "static_vms", ["static_vm_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_static_vm_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "static_vm_id")

    op.drop_table("static_vms")
