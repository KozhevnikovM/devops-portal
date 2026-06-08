"""bookings: add config_roles snapshot

Snapshot of the Ansible roles selected at order time — [{name, ansible_role, vars}] — applied to
the VM during configuration. Snapshotted so later catalog edits don't change a running VM
(v0.8.0 P2.2).

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("config_roles", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("bookings", "config_roles")
