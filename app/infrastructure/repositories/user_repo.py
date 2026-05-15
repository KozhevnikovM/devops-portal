import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.entities import APIKey, User
from app.infrastructure.database.models import APIKeyModel, UserModel


def _to_user(m: UserModel) -> User:
    return User(
        id=m.id,
        username=m.username,
        password_hash=m.password_hash,
        role=m.role,
        is_active=m.is_active,
        created_at=m.created_at,
    )


def _to_api_key(m: APIKeyModel) -> APIKey:
    return APIKey(
        id=m.id,
        key_hash=m.key_hash,
        user_id=m.user_id,
        description=m.description,
        is_active=m.is_active,
        created_at=m.created_at,
        last_used_at=m.last_used_at,
    )


class UserRepository:
    async def get(self, session: AsyncSession, user_id: uuid.UUID) -> User | None:
        result = await session.execute(
            select(UserModel).where(UserModel.id == user_id, UserModel.is_active == True)
        )
        m = result.scalar_one_or_none()
        return _to_user(m) if m else None

    async def get_by_username(self, session: AsyncSession, username: str) -> User | None:
        result = await session.execute(
            select(UserModel).where(UserModel.username == username, UserModel.is_active == True)
        )
        m = result.scalar_one_or_none()
        return _to_user(m) if m else None

    async def get_by_key_hash(self, session: AsyncSession, key_hash: str) -> User | None:
        result = await session.execute(
            select(UserModel)
            .join(APIKeyModel, APIKeyModel.user_id == UserModel.id)
            .where(
                APIKeyModel.key_hash == key_hash,
                APIKeyModel.is_active == True,
                UserModel.is_active == True,
            )
        )
        m = result.scalar_one_or_none()
        if not m:
            return None
        # Update last_used_at on the matching key
        key_result = await session.execute(
            select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
        )
        key_model = key_result.scalar_one_or_none()
        if key_model:
            key_model.last_used_at = datetime.now(timezone.utc)
            await session.commit()
        return _to_user(m)

    async def create(
        self,
        session: AsyncSession,
        username: str,
        password_hash: str,
        role: str,
    ) -> User:
        m = UserModel(
            id=uuid.uuid4(),
            username=username,
            password_hash=password_hash,
            role=role,
        )
        session.add(m)
        await session.commit()
        await session.refresh(m)
        return _to_user(m)

    async def list_all(self, session: AsyncSession) -> list[User]:
        result = await session.execute(select(UserModel).order_by(UserModel.created_at))
        return [_to_user(m) for m in result.scalars().all()]

    async def list_api_keys(self, session: AsyncSession, user_id: uuid.UUID) -> list[APIKey]:
        result = await session.execute(
            select(APIKeyModel)
            .where(APIKeyModel.user_id == user_id, APIKeyModel.is_active == True)
            .order_by(APIKeyModel.created_at)
        )
        return [_to_api_key(m) for m in result.scalars().all()]

    async def create_api_key(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        description: str | None,
    ) -> tuple[str, APIKey]:
        raw_key = f"dp_{secrets.token_hex(16)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        m = APIKeyModel(
            id=uuid.uuid4(),
            key_hash=key_hash,
            user_id=user_id,
            description=description,
        )
        session.add(m)
        await session.commit()
        await session.refresh(m)
        return raw_key, _to_api_key(m)

    async def revoke_api_key(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        key_id: uuid.UUID,
    ) -> bool:
        result = await session.execute(
            select(APIKeyModel).where(
                APIKeyModel.id == key_id,
                APIKeyModel.user_id == user_id,
            )
        )
        m = result.scalar_one_or_none()
        if not m:
            return False
        m.is_active = False
        await session.commit()
        return True

    # ── Sync helpers (used by startup seed) ──────────────────────────────────

    def sync_list_all(self, session: Session) -> list[User]:
        result = session.execute(select(UserModel))
        return [_to_user(m) for m in result.scalars().all()]

    def sync_create(
        self,
        session: Session,
        username: str,
        password_hash: str,
        role: str,
        user_id: uuid.UUID | None = None,
    ) -> User:
        m = UserModel(
            id=user_id or uuid.uuid4(),
            username=username,
            password_hash=password_hash,
            role=role,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return _to_user(m)
