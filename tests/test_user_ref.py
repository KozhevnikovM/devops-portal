"""Unit tests for the owner/creator username join helpers (#510).

The join compares the bare `users.id` with the reference converted to uuid, guarded so that only
canonical uuid text converts. Plans and name equality against real SQL live in
tests/integration/test_user_name_resolution.py.
"""

import ast
import re
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import aliased

from app.infrastructure.database.models import BookingModel, EnvironmentModel, UserModel
from app.infrastructure.repositories._user_ref import (
    _CANONICAL_UUID,
    user_by_ref,
    user_ref_for_username,
    user_ref_uuid,
)


def _sql(expr) -> str:
    return str(
        expr.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


_HEX5 = r"\[0123456789abcdef\]"
_GUARD = (
    r"CASE WHEN \(\(bookings\.user_id COLLATE \"C\"\) ~ '\^"
    + _HEX5
    + r"\{8\}-"
    + _HEX5
    + r"\{4\}-"
    + _HEX5
    + r"\{4\}-"
    + _HEX5
    + r"\{4\}-"
    + _HEX5
    + r"\{12\}\$'\) "
    r"THEN CAST\(bookings\.user_id AS UUID\) END"
)


def test_user_by_ref_compares_bare_users_id_with_guarded_uuid_cast():
    sql = _sql(user_by_ref(UserModel, BookingModel.user_id))
    assert re.fullmatch(r"users\.id = " + _GUARD, sql), sql


def test_user_by_ref_works_against_an_aliased_users_model():
    creator = aliased(UserModel)
    sql = _sql(user_by_ref(creator, EnvironmentModel.created_by))
    assert re.fullmatch(
        r"users_1\.id = CASE WHEN \(\(environments\.created_by COLLATE \"C\"\) ~ .+",
        sql,
    ), sql
    assert "CAST(environments.created_by AS UUID)" in sql


def test_guard_pattern_is_an_enumerated_class_without_ranges():
    # Regex bracket ranges are collation-dependent; the guard must not rely on them.
    assert "0-9" not in _CANONICAL_UUID and "a-f" not in _CANONICAL_UUID
    assert _CANONICAL_UUID.count("[0123456789abcdef]") == 5
    assert 'COLLATE "C"' in _sql(user_ref_uuid(BookingModel.created_by))


def test_guard_pattern_matches_exactly_canonical_uuid_text():
    # fullmatch: Python's `$` also matches before a trailing newline, PostgreSQL's does not.
    assert re.fullmatch(_CANONICAL_UUID, "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
    for value in (
        "dev-user",
        "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0",
        "{0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0}",
        "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f٣",
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1fä",
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0\n",
    ):
        assert not re.fullmatch(_CANONICAL_UUID, value), value


def test_user_ref_for_username_is_a_scalar_subquery_on_username():
    sql = _sql(BookingModel.user_id == user_ref_for_username("alice"))
    assert sql == (
        "bookings.user_id = (SELECT CAST(users.id AS VARCHAR) AS id \nFROM users \n"
        "WHERE users.username = 'alice')"
    ), sql


# --- the touched statements (#510 tasks 3.1, 3.2) ------------------------------------------------


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_booking_list_item_stmt_joins_users_by_primary_key():
    from app.infrastructure.repositories.booking_repo import _list_item_stmt

    sql = _compiled(_list_item_stmt())
    assert (
        "CAST(users.id AS VARCHAR)" not in sql
        and "CAST(users_1.id AS VARCHAR)" not in sql
    )
    assert 'users.id = CASE WHEN ((bookings.user_id COLLATE "C")' in sql
    assert 'users_1.id = CASE WHEN ((bookings.created_by COLLATE "C")' in sql


def test_environment_rows_join_users_by_primary_key():
    from app.infrastructure.repositories.environment_repo import _with_usernames

    sql = _compiled(_with_usernames())
    assert (
        "CAST(users.id AS VARCHAR)" not in sql
        and "CAST(users_1.id AS VARCHAR)" not in sql
    )
    assert 'users.id = CASE WHEN ((environments.user_id COLLATE "C")' in sql
    assert 'users_1.id = CASE WHEN ((environments.created_by COLLATE "C")' in sql


# --- regression guard (#510 design Decision 4) ----------------------------------------------------

_REPOSITORIES = (
    Path(__file__).resolve().parents[1] / "app" / "infrastructure" / "repositories"
)


def _users_id_text_casts(source: str) -> list[int]:
    """Lines of `cast(<UserModel or an alias of it>.id, String)` in a module's source."""
    tree = ast.parse(source)
    users = {"UserModel"} | {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "aliased"
        and node.value.args
        and getattr(node.value.args[0], "id", None) == "UserModel"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "cast"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "id"
        and getattr(node.args[0].value, "id", None) in users
        and getattr(node.args[1], "id", None) == "String"
    ]


def test_guard_detects_the_old_join_spelling():
    source = (
        "_Creator = aliased(UserModel)\n"
        "a = cast(UserModel.id, String) == BookingModel.user_id\n"
        "b = cast(_Creator.id, String) == BookingModel.created_by\n"
        "c = cast(BookingModel.user_id, String) == uid\n"
    )
    assert _users_id_text_casts(source) == [2, 3]


def test_no_repository_joins_users_through_a_text_cast_of_users_id():
    offenders = {
        path.name: lines
        for path in sorted(_REPOSITORIES.glob("*.py"))
        if path.name != "_user_ref.py"
        and (lines := _users_id_text_casts(path.read_text()))
    }
    assert offenders == {}, (
        "join users through _user_ref.user_by_ref (users_pkey), not CAST(users.id AS VARCHAR): "
        f"{offenders}"
    )
