"""Statement shapes of page reconciliation's batch reads (#497 D3, D3a).

Authorization is the query: the page's kinds and the Mine rule are predicates of the batch read,
so an id outside the scope simply doesn't come back. The environment child read is bounded per
environment and never selects a secret column. Real-SQL behaviour lives in
tests/integration/test_reconcile_batch_reads.py.
"""
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.infrastructure.repositories.booking_repo import _list_items_by_ids_stmt
from app.infrastructure.repositories.environment_repo import _bounded_children_stmt

_SECRETS = ("password", "ssh_key", "provisioning_log", "startup_script", "extra_vars",
            "config_roles", "secret")


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_booking_batch_read_mine_scopes_by_type_and_owner():
    sql = _sql(_list_items_by_ids_stmt([uuid4()], user_id="u1", resource_types=["VM", "STATIC_VM"]))
    assert "bookings.resource_type IN ('VM', 'STATIC_VM')" in sql
    assert "bookings.user_id = 'u1' OR bookings.created_by = 'u1'" in sql


def test_booking_batch_read_all_scopes_by_type_only():
    sql = _sql(_list_items_by_ids_stmt([uuid4()], user_id=None, resource_types=["NAMESPACE"]))
    assert "bookings.resource_type IN ('NAMESPACE')" in sql
    assert "bookings.created_by =" not in sql


def test_booking_batch_read_applies_no_released_or_label_filter():
    sql = _sql(_list_items_by_ids_stmt([uuid4()], user_id="u1", resource_types=["VM"]))
    assert "RELEASED" not in sql
    assert "ILIKE" not in sql.upper()


def test_list_page_hydration_is_unscoped():
    # list_page's keys are already scoped; its hydration is the same statement without predicates.
    sql = _sql(_list_items_by_ids_stmt([uuid4()]))
    assert "resource_type IN" not in sql
    assert "created_by =" not in sql


def test_environment_children_read_is_bounded_per_environment():
    sql = _sql(_bounded_children_stmt([uuid4(), uuid4()], 26))
    assert "LATERAL" in sql
    # The LIMIT sits inside the per-environment lateral, not on the whole result.
    assert sql.count("LIMIT") == 1
    assert sql.index("LATERAL") < sql.index("LIMIT 26") < sql.rindex(") AS children")
    assert "ORDER BY" not in sql   # an ORDER BY would sort every child before the LIMIT


def test_environment_children_read_selects_no_secret_column():
    sql = _sql(_bounded_children_stmt([uuid4()], 26)).lower()
    for column in _SECRETS:
        assert column not in sql, column
    assert "bookings.*" not in sql
