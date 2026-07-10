"""role_secret_vars: add secret_vars JSONB column to roles table

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("secret_vars", JSONB, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("roles", "secret_vars")
