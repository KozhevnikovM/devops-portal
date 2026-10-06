"""Resolving a stored owner/creator reference to its user by the users primary key (#510).

`bookings.user_id`/`created_by` and `environments.user_id`/`created_by` are strings, while
`users.id` is a uuid. Joining on `CAST(users.id AS VARCHAR) = ref` casts the key side, so
users_pkey cannot serve the join and every joined alias reads the whole users table. These
helpers put the conversion on the reference side instead, leaving the bare `users.id` for a
per-row primary-key probe (openspec user-name-resolution).
"""

from sqlalchemy import String, case, cast, select
from sqlalchemy.dialects.postgresql import UUID

from app.infrastructure.database.models import UserModel

# Exactly the text `CAST(uuid AS VARCHAR)` produces: lowercase, hyphenated, 36 ASCII characters.
# The class is enumerated rather than written as `0-9a-f` ranges, and matched under the C
# collation, because PostgreSQL documents regex bracket ranges as collation-dependent.
_HEX = "[0123456789abcdef]"
_CANONICAL_UUID = f"^{_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}$"


def user_ref_uuid(ref):
    """The users.id a stored owner/creator reference names, or NULL when it names none.

    Any value that is not canonical uuid text — legacy pre-auth `dev-user`, NULL, uppercase or
    braced spellings — gives NULL, as it matched no user before. PostgreSQL evaluates only the
    selected CASE branch for a column reference, so the cast never sees a non-matching string.
    """
    return case(
        (ref.collate("C").regexp_match(_CANONICAL_UUID), cast(ref, UUID(as_uuid=True))),
        else_=None,
    )


def user_by_ref(user_model, ref):
    """Join condition: `user_model` (UserModel or an alias of it) is the user `ref` names."""
    return user_model.id == user_ref_uuid(ref)


def user_ref_for_username(username: str):
    """The stored-reference form (canonical id text) of the user named `username`, or NULL.

    A scalar subquery evaluated once through users_username_key. Casting the single resolved
    id is the one legitimate users-id-to-text cast; joins use `user_by_ref`.
    """
    return (
        select(cast(UserModel.id, String))
        .where(UserModel.username == username)
        .scalar_subquery()
    )
