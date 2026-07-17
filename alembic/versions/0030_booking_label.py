"""add label column to bookings

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('label', sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'label')
