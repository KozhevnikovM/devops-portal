"""bookings: add startup_script (post-provision bash config)

Adds a nullable per-booking bash script run over SSH in the CONFIGURING state after a VM is
provisioned (v0.8.0 P1.2).

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("startup_script", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "startup_script")
