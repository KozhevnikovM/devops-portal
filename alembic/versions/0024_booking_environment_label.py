"""bookings.environment_label: the blueprint item label for an environment child

Adds a nullable label carried from the blueprint item onto each child booking (#224), so the
Environments UI shows the admin's label (e.g. "web") instead of the bare resource type.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("environment_label", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "environment_label")
