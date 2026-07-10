"""drop namespace_shares: permanently remove the namespace sharing table

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-02
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("namespace_shares")


def downgrade() -> None:
    op.create_table(
        "namespace_shares",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "booking_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "shared_with_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "booking_id", "shared_with_user_id",
            name="uq_namespace_shares_booking_user",
        ),
    )
