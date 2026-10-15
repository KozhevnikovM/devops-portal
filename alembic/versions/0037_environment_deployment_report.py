"""Add nullable deployment report columns without defaults or backfill.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("deployment_ref", sa.String(512)),
    ("deployment_commit", sa.String(64)),
    ("deployment_status", sa.String(16)),
    ("deployment_frontend_url", sa.String(2048)),
    ("deployment_admin_url", sa.String(2048)),
    ("deployment_updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    # Bound the exclusive lock acquisition on a busy production table. Retry deployment if busy.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, column_type in _COLUMNS:
        op.add_column("environments", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("environments", name)
