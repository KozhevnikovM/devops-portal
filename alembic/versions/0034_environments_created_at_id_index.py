"""index environments (created_at, id) for keyset pagination

The browser environments page is paginated with a (created_at, id) keyset cursor ordered
created_at DESC, id DESC (#467). A backward scan of this index serves that order and starts at
the cursor, so a page never sorts the full matching set. environments is small, so a plain
(non-concurrent) build is fine.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-25
"""
from alembic import op

revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_environments_created_at_id', 'environments', ['created_at', 'id'])


def downgrade() -> None:
    op.drop_index('ix_environments_created_at_id', table_name='environments')
