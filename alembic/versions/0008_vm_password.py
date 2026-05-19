"""bookings: add vm_password column

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("vm_password", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "vm_password")
