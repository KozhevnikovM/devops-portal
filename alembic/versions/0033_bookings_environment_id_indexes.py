"""index bookings.environment_id (full + partial on non-RELEASED children)

The environments list excludes fully released environments in SQL with EXISTS / NOT EXISTS probes
over bookings.environment_id (#466). The full index serves "has any child" and every child lookup
by environment; the partial one serves "has a non-RELEASED child" without touching released
history. bookings is small, so a plain (non-concurrent) build is fine.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-25
"""
import sqlalchemy as sa

from alembic import op

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_bookings_environment_id', 'bookings', ['environment_id'])
    op.create_index(
        'ix_bookings_environment_id_unreleased', 'bookings', ['environment_id'],
        postgresql_where=sa.text("status <> 'RELEASED'"),
    )


def downgrade() -> None:
    op.drop_index('ix_bookings_environment_id_unreleased', table_name='bookings')
    op.drop_index('ix_bookings_environment_id', table_name='bookings')
