"""environment blueprints: admin templates bundling resources into a stack

Adds environment_blueprints + environment_blueprint_items (v0.8.0 P3.1). Ordering a blueprint
(parent Environment + child bookings) comes in a later revision.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_blueprints",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "environment_blueprint_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("blueprint_id", sa.UUID(), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("spec", JSONB, nullable=False, server_default="{}"),
    )
    op.create_foreign_key(
        "fk_blueprint_items_blueprint_id", "environment_blueprint_items",
        "environment_blueprints", ["blueprint_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_table("environment_blueprint_items")
    op.drop_table("environment_blueprints")
