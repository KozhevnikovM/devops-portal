"""Shared authorization predicates for managing bookings/environments.

A resource is managed by its **owner** (`user_id`), the **dispatcher that created it** on the owner's
behalf (`created_by`, from #229), or an **admin**. `created_by` is only ever a dispatcher/admin id, so
for an ordinary user this collapses to plain ownership.
"""
from app.domain.entities import User


def can_manage(*, owner_id: str, created_by: str | None, user: User) -> bool:
    """True if `user` may view/release/extend a resource owned by `owner_id`/created by `created_by`."""
    return user.role == "admin" or str(user.id) in {owner_id, created_by}


def can_view_credentials(*, owner_id: str, user: User) -> bool:
    """True if `user` may see a booking's credentials: its owner or an admin (#478).

    Deliberately narrower than `can_manage` — the dispatcher who created the booking on the owner's
    behalf may manage its lifecycle but never sees its secrets.
    """
    return user.role == "admin" or str(user.id) == owner_id
