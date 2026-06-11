import hashlib
import json
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import User
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.user_repo import UserRepository

_redis: aioredis.Redis | None = None

_user_repo = UserRepository()


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> User | None:
    # API key auth: Authorization: Bearer dp_...
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw_key = auth[7:]
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        return await _user_repo.get_by_key_hash(session, key_hash)

    # Session cookie auth
    session_id = request.cookies.get("session_id")
    if session_id:
        r = _get_redis()
        data = await r.get(f"session:{session_id}")
        if data:
            payload = json.loads(data)
            return await _user_repo.get(session, UUID(payload["user_id"]))

    return None


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> User:
    user = await get_current_user(request, session)
    if user is None:
        if "application/json" in request.headers.get("accept", ""):
            raise HTTPException(status_code=401, detail="Not authenticated")
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# The roles an admin may assign when creating a user. `dispatcher` may order on behalf of others
# (#229); `admin` ⊇ dispatcher ⊇ user for capabilities.
VALID_ROLES = {"user", "dispatcher", "admin"}
