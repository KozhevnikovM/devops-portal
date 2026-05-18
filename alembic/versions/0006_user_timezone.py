"""add timezone column to users

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    op.drop_column("users", "timezone")
