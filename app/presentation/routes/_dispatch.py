"""Shared helper for dispatcher 'on behalf of' ordering (#229).

Resolves who an order is *for* (the owner) and who *placed* it (the acting dispatcher), enforcing
that only a dispatcher/admin may order for someone else and that the target user exists + is active.
"""
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.infrastructure.repositories.user_repo import UserRepository

_user_repo = UserRepository()

_DISPATCH_ROLES = {"dispatcher", "admin"}


async def resolve_owner(
    session: AsyncSession, current_user: User, on_behalf_of: str | None,
) -> tuple[str, str | None]:
    """Return (owner_id, created_by) for an order.

    - No ``on_behalf_of`` → a normal self-order: (caller id, None).
    - With ``on_behalf_of`` → only a dispatcher/admin may set it (403 otherwise); the target username
      must resolve to an active user (400 otherwise). Returns (target id, caller id).
    """
    if not on_behalf_of:
        return str(current_user.id), None
    if current_user.role not in _DISPATCH_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only a dispatcher may order on behalf of another user",
        )
    target = await _user_repo.get_by_username(session, on_behalf_of)
    if target is None or not target.is_active:
        raise HTTPException(status_code=400, detail=f"no such active user '{on_behalf_of}'")
    return str(target.id), str(current_user.id)
