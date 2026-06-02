"""namespace inventory catalog + booking resource_type

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "namespaces",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(63), nullable=False, unique=True),
        sa.Column("cluster_name", sa.String(64), nullable=False),
        sa.Column("api_url", sa.String(256), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.add_column(
        "bookings",
        sa.Column("resource_type", sa.String(16), nullable=False, server_default="VM"),
    )
    op.add_column("bookings", sa.Column("namespace_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_namespace_id", "bookings", "namespaces", ["namespace_id"], ["id"]
    )

    # Relax VM-specific columns — NULL for namespace bookings.
    op.alter_column("bookings", "image_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("bookings", "image_name", existing_type=sa.String(64), nullable=True)
    op.alter_column("bookings", "hw_config_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("bookings", "hw_config_name", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    op.alter_column("bookings", "hw_config_name", existing_type=sa.String(64), nullable=False)
    op.alter_column("bookings", "hw_config_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("bookings", "image_name", existing_type=sa.String(64), nullable=False)
    op.alter_column("bookings", "image_id", existing_type=sa.UUID(), nullable=False)

    op.drop_constraint("fk_bookings_namespace_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "namespace_id")
    op.drop_column("bookings", "resource_type")

    op.drop_table("namespaces")
