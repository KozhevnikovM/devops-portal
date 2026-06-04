"""bookings: add drive_type (backfill HDD), rename hdd_mb -> disk_mb

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default backfills every existing booking to HDD (the prior implicit behaviour).
    op.add_column(
        "bookings",
        sa.Column("drive_type", sa.String(8), nullable=False, server_default="HDD"),
    )
    op.alter_column("bookings", "hdd_mb", new_column_name="disk_mb")


def downgrade() -> None:
    op.alter_column("bookings", "disk_mb", new_column_name="hdd_mb")
    op.drop_column("bookings", "drive_type")
