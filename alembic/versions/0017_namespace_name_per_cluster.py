"""namespaces: name unique per-cluster instead of globally

Drops the global UNIQUE on namespaces.name and replaces it with a composite UNIQUE on
(name, cluster_name), so the same namespace name may exist on different clusters and the
(name, cluster) pair becomes the natural identifier.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-05
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# Postgres auto-named the inline column UNIQUE from 0012 (`name`, unique=True).
_OLD_NAME_UNIQUE = "namespaces_name_key"
_NEW_COMPOSITE_UNIQUE = "uq_namespaces_name_cluster"


def upgrade() -> None:
    op.drop_constraint(_OLD_NAME_UNIQUE, "namespaces", type_="unique")
    op.create_unique_constraint(
        _NEW_COMPOSITE_UNIQUE, "namespaces", ["name", "cluster_name"]
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_COMPOSITE_UNIQUE, "namespaces", type_="unique")
    op.create_unique_constraint(_OLD_NAME_UNIQUE, "namespaces", ["name"])
