"""add construction_complete column to environments

An environment's lease must not start while its order is still creating children (#434). Existing
rows are fully constructed, so they are backfilled true; new rows start false until the order
marks them complete.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-25
"""
import sqlalchemy as sa

from alembic import op

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'environments',
        sa.Column('construction_complete', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column('environments', 'construction_complete', server_default=sa.false())


def downgrade() -> None:
    op.drop_column('environments', 'construction_complete')
