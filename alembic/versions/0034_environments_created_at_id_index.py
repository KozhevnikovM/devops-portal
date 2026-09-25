"""index environments (created_at, id) for keyset pagination

The browser environments page is paginated with a (created_at, id) keyset cursor ordered
created_at DESC, id DESC (#467). A backward scan of this index serves that order from the cursor
with no sort; the unfiltered list uses it and reads at most limit + 1 rows. For selective filters
(Mine / label / hidden released) the planner may still prefer a seq scan with a top-N sort when
that is cheaper (see the change's design.md, Decision 8). environments is small, so a plain
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
