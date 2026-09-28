"""index bookings for bounded keyset pages and queue rank

The browser bookings pages are paginated with a (created_at, id) keyset cursor ordered
created_at DESC, id DESC (#479). Page selection runs one ordered walk per branch: a page scope
(All by resource type, Mine by owner, Mine by creator) × RELEASED rows hidden or shown. Each branch
has its own index, led by its own page-key expression (e.g. 'ol:' || user_id || ':' ||
resource_type) and then (created_at, id). The branch's query constrains exactly that expression,
so no broader ordered index can serve it and every entry it walks matches the page: a page reads
at most limit + 1 entries per branch. ix_bookings_queued_rank makes the FIFO queue-position count
read only QUEUED rows. The expressions and predicates are spelled exactly as the queries spell
them (see booking_page_key in app/infrastructure/database/models.py; an integration test checks
the two produce identical index definitions).

The application version that ships this migration runs its page query with sequential and bitmap
scans disabled (design.md, Decision 10), so this migration must be applied BEFORE that version
starts serving. A plain (non-concurrent) build, like 0033: bookings is small enough that the lock
is brief.

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

# (name, key expression, partial predicate or None); every page index is (key, created_at, id)
_PAGE_INDEXES = [
    ('ix_bookings_type_page', "'t:' || resource_type", None),
    ('ix_bookings_type_page_unreleased', "'tl:' || resource_type", _NOT_RELEASED),
    ('ix_bookings_owner_page', "'o:' || user_id || ':' || resource_type", None),
    ('ix_bookings_owner_page_unreleased', "'ol:' || user_id || ':' || resource_type", _NOT_RELEASED),
    ('ix_bookings_creator_page', "'c:' || created_by || ':' || resource_type", _CREATOR),
    ('ix_bookings_creator_page_unreleased', "'cl:' || created_by || ':' || resource_type",
     f"{_CREATOR} AND {_NOT_RELEASED}"),
]


def upgrade() -> None:
    for name, key, where in _PAGE_INDEXES:
        op.create_index(
            name, 'bookings', [sa.text(f"({key})"), 'created_at', 'id'],
            postgresql_where=sa.text(where) if where else None,
        )
    op.create_index(
        'ix_bookings_queued_rank', 'bookings', ['resource_type', 'created_at'],
        postgresql_where=sa.text("status = 'QUEUED'"),
    )


def downgrade() -> None:
    op.drop_index('ix_bookings_queued_rank', table_name='bookings')
    for name, _, _ in reversed(_PAGE_INDEXES):
        op.drop_index(name, table_name='bookings')
