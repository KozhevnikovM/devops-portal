"""booking ttl unit: hours → minutes

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-15
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("bookings", "ttl_hours", new_column_name="ttl_minutes")
    op.execute("UPDATE bookings SET ttl_minutes = ttl_minutes * 60")


def downgrade() -> None:
    op.execute("UPDATE bookings SET ttl_minutes = ttl_minutes / 60")
    op.alter_column("bookings", "ttl_minutes", new_column_name="ttl_hours")
