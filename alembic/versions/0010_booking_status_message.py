"""bookings: add status_message column

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("status_message", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "status_message")
