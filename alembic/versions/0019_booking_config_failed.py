"""bookings: add config_failed flag

A VM that is reachable but whose post-provision configuration script failed is still usable, so it
goes READY with config_failed=true (rather than FAILED). v0.8.0 P1.2.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("config_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("bookings", "config_failed")
