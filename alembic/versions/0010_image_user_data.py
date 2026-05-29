"""vm_images: add user_data column

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vm_images", sa.Column("user_data", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vm_images", "user_data")
