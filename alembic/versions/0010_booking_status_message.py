"""bookings: add status_message column

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS handles re-runs where the column was added as VARCHAR(128) before
    # this migration was corrected to use TEXT.
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS status_message TEXT")
    op.execute("ALTER TABLE bookings ALTER COLUMN status_message TYPE TEXT USING status_message::TEXT")


def downgrade() -> None:
    op.drop_column("bookings", "status_message")
