"""booking_extra_vars: add extra_vars JSONB column to bookings

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("extra_vars", JSONB, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("bookings", "extra_vars")
