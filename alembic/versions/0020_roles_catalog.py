"""roles catalog: Ansible roles offered for VM configuration

Adds the `roles` table — admin-managed catalog entries pointing at an Ansible role with default
variables, applied to a VM during configuration (v0.8.0 P2.1).

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("ansible_role", sa.String(128), nullable=False),
        sa.Column("default_vars", JSONB, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("roles")
