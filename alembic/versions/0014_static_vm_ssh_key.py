"""static VM ssh_key credential (password now optional)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("static_vms", sa.Column("ssh_key", sa.Text(), nullable=True))
    op.alter_column("static_vms", "password", existing_type=sa.String(256), nullable=True)
    op.create_check_constraint(
        "ck_static_vms_credential_present",
        "static_vms",
        "password IS NOT NULL OR ssh_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_static_vms_credential_present", "static_vms", type_="check")
    op.alter_column("static_vms", "password", existing_type=sa.String(256), nullable=False)
    op.drop_column("static_vms", "ssh_key")
