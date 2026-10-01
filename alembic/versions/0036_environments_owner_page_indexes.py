"""index environments by owner and by creator for Mine keyset pages

The environments page's Mine filter is the union of the environments a user owns (user_id) and the
ones they dispatched on someone's behalf (created_by) (#496). No single index returns an OR of two
columns in (created_at, id) order, so Mine page selection runs one keyset walk per scope, each on
its own index led by that column. On that path the read is bounded by the viewer's own history
instead of everyone's. The creator index is partial because most environments are not dispatched;
`created_by = :me` implies its predicate.

A plain (non-concurrent) build, like 0033–0035: environments is written once per order and is
small enough that the lock is brief. The application stays correct without these indexes (each
walk then filters ix_environments_created_at_id), so the build order relative to the deploy only
affects cost.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-01
"""
import sqlalchemy as sa

from alembic import op

revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None

# (name, leading column, partial predicate or None); every page index is (column, created_at, id)
_PAGE_INDEXES = [
    ('ix_environments_owner_page', 'user_id', None),
    ('ix_environments_creator_page', 'created_by', "created_by IS NOT NULL"),
]


def upgrade() -> None:
    for name, column, where in _PAGE_INDEXES:
        op.create_index(
            name, 'environments', [column, 'created_at', 'id'],
            postgresql_where=sa.text(where) if where else None,
        )


def downgrade() -> None:
    for name, _, _ in reversed(_PAGE_INDEXES):
        op.drop_index(name, table_name='environments')
