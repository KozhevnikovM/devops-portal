"""Shared role-name resolution for booking creation (API and HTML form entry points).

Resolves catalog role names into the `config_roles` snapshot format `CreateBookingUseCase`
expects — captured at order time so a later catalog edit doesn't change an already-running VM.
"""
from app.config import settings
from app.domain.exceptions import RoleNotFoundError


async def resolve_config_roles(session, role_repo, role_names: list[str]) -> list[dict]:
    """Role names -> a snapshot list, or raises RoleNotFoundError on an unknown name."""
    config_roles = []
    for role_name in role_names:
        role = await role_repo.get_by_name(session, role_name)
        if role is None:
            raise RoleNotFoundError(f"no role named '{role_name}'")
        config_roles.append({
            "name": role.name,
            "ansible_role": role.ansible_role,
            "vars": role.default_vars or {},
            "secret_vars": role.secret_vars if settings.SECRET_VARS_ENABLED else {},
        })
    return config_roles
