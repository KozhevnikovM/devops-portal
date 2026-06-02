"""add default image & hw config columns to users

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("default_image_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("default_hw_config_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_default_image_id",
        "users", "vm_images",
        ["default_image_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_users_default_hw_config_id",
        "users", "hw_configs",
        ["default_hw_config_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_default_hw_config_id", "users", type_="foreignkey")
    op.drop_constraint("fk_users_default_image_id", "users", type_="foreignkey")
    op.drop_column("users", "default_hw_config_id")
    op.drop_column("users", "default_image_id")
