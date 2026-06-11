"""created_by on bookings + environments: the acting dispatcher when ordered on behalf of the owner

v0.9.0 P1 (#229). `user_id` stays the owner; `created_by` records who actually placed the order
(a dispatcher), null for a normal self-order.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("created_by", sa.String(64), nullable=True))
    op.add_column("environments", sa.Column("created_by", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("environments", "created_by")
    op.drop_column("bookings", "created_by")
