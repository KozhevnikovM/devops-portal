"""index bookings for bounded keyset pages and queue rank

The browser bookings pages are paginated with a (created_at, id) keyset cursor ordered
created_at DESC, id DESC (#479). Page selection runs one ordered walk per owner column and
resource type. These indexes put those equality filters ahead of (created_at, id), so every entry
a walk visits matches the page and a page reads at most limit + 1 entries per walk. Each comes
full (Show released) and without RELEASED rows (the default view). ix_bookings_queued_rank makes
the FIFO queue-position count read only QUEUED rows. The predicates are spelled exactly as the
queries spell them (see BOOKING_NOT_RELEASED_SQL in app/infrastructure/database/models.py).
A plain (non-concurrent) build, like 0033: bookings is small enough that the lock is brief.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-28
"""
import sqlalchemy as sa

from alembic import op

revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None

_NOT_RELEASED = "status <> 'RELEASED'"
_CREATOR = "created_by IS NOT NULL"

# (name, columns, partial predicate or None)
_INDEXES = [
    ('ix_bookings_type_page', ['resource_type', 'created_at', 'id'], None),
    ('ix_bookings_type_page_unreleased', ['resource_type', 'created_at', 'id'], _NOT_RELEASED),
    ('ix_bookings_owner_page', ['user_id', 'resource_type', 'created_at', 'id'], None),
    ('ix_bookings_owner_page_unreleased', ['user_id', 'resource_type', 'created_at', 'id'], _NOT_RELEASED),
    ('ix_bookings_creator_page', ['created_by', 'resource_type', 'created_at', 'id'], _CREATOR),
    ('ix_bookings_creator_page_unreleased', ['created_by', 'resource_type', 'created_at', 'id'],
     f"{_CREATOR} AND {_NOT_RELEASED}"),
    ('ix_bookings_queued_rank', ['resource_type', 'created_at'], "status = 'QUEUED'"),
]


def upgrade() -> None:
    for name, columns, where in _INDEXES:
        op.create_index(
            name, 'bookings', columns,
            postgresql_where=sa.text(where) if where else None,
        )


def downgrade() -> None:
    for name, _, _ in reversed(_INDEXES):
        op.drop_index(name, table_name='bookings')
