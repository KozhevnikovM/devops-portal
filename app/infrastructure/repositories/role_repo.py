import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Role
from app.infrastructure.crypto import encrypt_dict
from app.infrastructure.database.models import RoleModel

logger = logging.getLogger(__name__)


def _to_entity(m: RoleModel) -> Role:
    return Role(
        id=m.id,
        name=m.name,
        description=m.description,
        ansible_role=m.ansible_role,
        default_vars=m.default_vars or {},
        secret_vars=m.secret_vars or {},
        is_active=m.is_active,
        created_at=m.created_at,
    )


def _encrypt_secret_vars(raw: dict) -> dict:
    """Encrypt *raw* using the configured key; raises ValueError if key is absent and dict is non-empty."""
    return encrypt_dict(raw, settings.SECRETS_ENCRYPTION_KEY)


class RoleRepository:
    async def list_all(self, session: AsyncSession) -> list[Role]:
        result = await session.execute(select(RoleModel).order_by(RoleModel.name))
        return [_to_entity(m) for m in result.scalars().all()]

    async def list_active(self, session: AsyncSession) -> list[Role]:
        result = await session.execute(
            select(RoleModel).where(RoleModel.is_active.is_(True)).order_by(RoleModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def get(self, session: AsyncSession, role_id: UUID) -> Role:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise ValueError(f"Role {role_id} not found")
        return _to_entity(model)

    async def get_by_name(self, session: AsyncSession, name: str) -> Role | None:
        """Resolve an *active* role by its (unique) name; None if no active match."""
        result = await session.execute(
            select(RoleModel).where(RoleModel.name == name, RoleModel.is_active.is_(True))
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def create(
        self, session: AsyncSession, name: str, description: str | None,
        ansible_role: str, default_vars: dict, secret_vars: dict | None = None,
        actor: str = "unknown",
    ) -> Role:
        encrypted = _encrypt_secret_vars(secret_vars or {})
        model = RoleModel(
            id=uuid4(), name=name, description=description or None,
            ansible_role=ansible_role, default_vars=default_vars or {},
            secret_vars=encrypted,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        if encrypted:
            logger.info(
                "role_secret_vars_changed",
                extra={"event": "role_secret_vars_changed", "actor": actor,
                       "role_id": str(model.id), "keys": sorted(encrypted.keys())},
            )
        return _to_entity(model)

    async def update(self, session: AsyncSession, role_id: UUID, fields: dict,
                     actor: str = "unknown") -> Role:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise ValueError(f"Role {role_id} not found")
        secret_vars = fields.pop("secret_vars", None)
        for key, value in fields.items():
            setattr(model, key, value)
        if secret_vars is not None:
            encrypted = _encrypt_secret_vars(secret_vars)
            model.secret_vars = encrypted
            if encrypted:
                logger.info(
                    "role_secret_vars_changed",
                    extra={"event": "role_secret_vars_changed", "actor": actor,
                           "role_id": str(role_id), "keys": sorted(encrypted.keys())},
                )
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise ValueError(f"Role {role_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise ValueError(f"Role {role_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise ValueError(f"Role {role_id} not found")
        await session.delete(model)
        await session.commit()
