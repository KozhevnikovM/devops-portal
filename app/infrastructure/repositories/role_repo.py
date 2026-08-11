import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.entities import Role
from app.domain.exceptions import RoleNotFoundError
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


# The UI masks existing secret values as ●●● and submits this same string for any key the admin
# left untouched (ansible_vars_editor.js). Keep both sides in sync.
SECRET_UNCHANGED_SENTINEL = "●●●"  # ●●●


def _merge_secret_vars(existing_encrypted: dict, submitted: dict) -> tuple[dict, list[str]]:
    """Merge a submitted secret_vars mapping against the stored (encrypted) one, per-key.

    A submitted value equal to the sentinel ●●● for a key that already exists reuses that key's
    stored ciphertext verbatim (so the client never needs the plaintext of secrets it isn't
    changing). Any other value is a new plaintext secret to encrypt. Keys absent from *submitted*
    are dropped. Returns the new encrypted dict plus the keys whose value was (re)encrypted.
    """
    preserved: dict = {}
    to_encrypt: dict = {}
    for key, value in submitted.items():
        if value == SECRET_UNCHANGED_SENTINEL and key in existing_encrypted:
            preserved[key] = existing_encrypted[key]
        else:
            to_encrypt[key] = value
    return {**preserved, **_encrypt_secret_vars(to_encrypt)}, list(to_encrypt.keys())


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
            raise RoleNotFoundError(f"Role {role_id} not found")
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
            raise RoleNotFoundError(f"Role {role_id} not found")
        secret_vars = fields.pop("secret_vars", None)
        for key, value in fields.items():
            setattr(model, key, value)
        if secret_vars is not None:
            merged, changed_keys = _merge_secret_vars(model.secret_vars or {}, secret_vars)
            model.secret_vars = merged
            if changed_keys:
                logger.info(
                    "role_secret_vars_changed",
                    extra={"event": "role_secret_vars_changed", "actor": actor,
                           "role_id": str(role_id), "keys": sorted(changed_keys)},
                )
        await session.commit()
        await session.refresh(model)
        return _to_entity(model)

    async def activate(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise RoleNotFoundError(f"Role {role_id} not found")
        model.is_active = True
        await session.commit()

    async def deactivate(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise RoleNotFoundError(f"Role {role_id} not found")
        model.is_active = False
        await session.commit()

    async def delete(self, session: AsyncSession, role_id: UUID) -> None:
        model = await session.get(RoleModel, role_id)
        if model is None:
            raise RoleNotFoundError(f"Role {role_id} not found")
        await session.delete(model)
        await session.commit()
