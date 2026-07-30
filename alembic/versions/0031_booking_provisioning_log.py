"""add provisioning_log column to bookings

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('provisioning_log', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'provisioning_log')
