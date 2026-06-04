"""hw_configs: add drive_type, rename hdd_mb -> disk_mb

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hw_configs",
        sa.Column("drive_type", sa.String(8), nullable=False, server_default="HDD"),
    )
    op.alter_column("hw_configs", "hdd_mb", new_column_name="disk_mb")


def downgrade() -> None:
    op.alter_column("hw_configs", "disk_mb", new_column_name="hdd_mb")
    op.drop_column("hw_configs", "drive_type")
