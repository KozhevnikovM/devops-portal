"""environments: parent of a stack of child bookings

Adds the environments table + bookings.environment_id (v0.8.0 P3.2). Ordering a blueprint creates
one Environment row + N child bookings tagged with environment_id.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("blueprint_name", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("ttl_minutes", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("bookings", sa.Column("environment_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_environment_id", "bookings", "environments",
        ["environment_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_environment_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "environment_id")
    op.drop_table("environments")
