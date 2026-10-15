"""JSON API for ordering and viewing environments (a stack of child bookings)."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.deployment import DeploymentReport
from app.domain.entities import Environment, User
from app.domain.enums import ResourceType
from app.domain.environment_status import derive_environment_status
from app.domain.exceptions import (
    BlueprintNotFoundError,
    BookingPermissionError,
    EnvironmentError,
    EnvironmentItemError,
    EnvironmentNotFoundError,
    InvalidExtensionError,
    NamespaceUnavailableError,
    NotFoundError,
    QuotaExceededError,
    StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation import deps
from app.presentation.middleware.correlation_id import get_request_id
from app.presentation.routes._dispatch import resolve_owner

router = APIRouter(prefix="/environments", tags=["environments"])

# Shared singletons from the composition root. Names kept so existing patches still target them.
_repo = deps.booking_repo
_env_repo = deps.env_repo
_blueprint_repo = deps.blueprint_repo
_image_repo = deps.image_repo
_hw_config_repo = deps.hw_config_repo
_role_repo = deps.role_repo
_namespace_repo = deps.namespace_repo
_static_vm_repo = deps.static_vm_repo
_dispatcher = deps.dispatcher
_create_use_case = deps.create_booking_uc
_reserve_static_vm_use_case = deps.reserve_static_vm_uc
_book_namespace_use_case = deps.book_namespace_uc
_order_use_case = deps.order_environment_uc
_release_use_case = deps.release_environment_uc
_update_name_use_case = deps.update_environment_name_uc
_extend_use_case = deps.extend_environment_uc
_update_deployment_use_case = deps.update_environment_deployment_uc


class UpdateEnvironmentDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    deployment_ref: str = Field(min_length=1, max_length=512)
    deployment_commit: str = Field(
        min_length=40, max_length=64, pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
    )
    deployment_status: Literal["DEPLOYING", "SUCCESS", "FAILED"]
    deployment_frontend_url: str = Field(min_length=1, max_length=2048)
    deployment_admin_url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_report(self):
        self.to_report()
        return self

    def to_report(self) -> DeploymentReport:
        return DeploymentReport(**self.model_dump())


class UpdateEnvironmentRequest(BaseModel):
    name: str


class ExtendEnvironmentRequest(BaseModel):
    # Minutes to add to the whole stack's lease. Positive only: unlike a booking, an environment
    # extension never makes the lease permanent (#532). Strict, so JSON 1.0 / "1" / true are
    # rejected (422) rather than coerced to an int that would extend the whole stack.
    extend_minutes: int = Field(strict=True, gt=0)


class OrderEnvironmentRequest(BaseModel):
    blueprint_name: str
    ttl_minutes: int
    # Dispatcher only: order on behalf of this user (username); the environment is owned by them.
    on_behalf_of: str | None = None
    # Order-time override of the blueprint's single namespace item (#235); both or neither.
    namespace_name: str | None = None
    cluster_name: str | None = None
    # Order-time Ansible var overrides for VM items, keyed by the item's blueprint label
    # (#352), e.g. {"web": {"git_branch": "feature-x"}}. Overrides the blueprint's own
    # per-item vars on key conflict; items not named here are unaffected.
    item_vars: dict[str, dict] | None = None


def _derived_status(env: Environment) -> str:
    """Aggregate the environment's status from its children (rules live in the domain, #434)."""
    return derive_environment_status(b.status for b in env.bookings).value


def _serialize(env: Environment) -> dict:
    return {
        "id": str(env.id),
        "name": env.name,
        "blueprint_name": env.blueprint_name,
        "status": _derived_status(env),
        "owner_username": env.owner_username,
        "created_by": env.created_by,
        "ttl_minutes": env.ttl_minutes,
        "expires_at": env.expires_at.isoformat(),
        "created_at": env.created_at.isoformat(),
        "deployment_ref": env.deployment_ref,
        "deployment_commit": env.deployment_commit,
        "deployment_status": env.deployment_status,
        "deployment_frontend_url": env.deployment_frontend_url,
        "deployment_admin_url": env.deployment_admin_url,
        "deployment_updated_at": env.deployment_updated_at.isoformat()
        if env.deployment_updated_at
        else None,
        "bookings": [
            {
                "id": str(b.id),
                "label": b.environment_label,
                "resource_type": b.resource_type.value,
                "status": b.status.value,
                "config_failed": b.config_failed,
                "image_name": b.image_name,
                "hw_config_name": b.hw_config_name,
                "namespace": b.namespace_name,
                "static_vm": b.static_vm_name,
                "vm_ip": b.vm_ip,
                "roles": [r.get("name") for r in (b.config_roles or [])],
            }
            for b in env.bookings
        ],
    }


@router.post("", status_code=201)
async def order_environment(
    body: OrderEnvironmentRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    owner_id, created_by = await resolve_owner(session, current_user, body.on_behalf_of)
    # A (name, cluster) pair identifies a namespace; both must be given together.
    if bool(body.namespace_name) != bool(body.cluster_name):
        raise HTTPException(
            status_code=400,
            detail="namespace_name and cluster_name must be provided together",
        )
    try:
        env = await _order_use_case.execute(
            session,
            body.blueprint_name,
            body.ttl_minutes,
            user_id=owner_id,
            created_by=created_by,
            namespace_name=body.namespace_name,
            cluster_name=body.cluster_name,
            item_vars=body.item_vars,
            request_id=get_request_id(),
        )
    except BlueprintNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except EnvironmentItemError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (
        QuotaExceededError,
        NamespaceUnavailableError,
        StaticVMUnavailableError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    env.owner_username = body.on_behalf_of or current_user.username
    return _serialize(env)


@router.get("")
async def list_environments(
    label: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if current_user.role == "admin":
        envs = await _env_repo.list_all(session, label=label)
    else:
        envs = await _env_repo.list_by_user(session, str(current_user.id), label=label)
    return [_serialize(e) for e in envs]


@router.get("/by-namespace/{namespace_name}")
async def get_environment_by_namespace(
    namespace_name: str,
    cluster: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Locate the live environment whose namespace child is named `namespace_name`.

    Lets any authenticated pipeline or user find a stack by namespace instead of environment id.
    Optional `cluster` disambiguates a name reused across clusters. Any authenticated caller → 200;
    not found / free / standalone namespace → 404.
    """
    envs = await _env_repo.get_by_namespace(
        session, namespace_name, cluster_name=cluster
    )
    if not envs:
        raise HTTPException(
            status_code=404,
            detail=f"no active environment with namespace '{namespace_name}'",
        )
    if len(envs) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"namespace '{namespace_name}' is ambiguous across clusters; specify ?cluster=",
        )
    return _serialize(envs[0])


@router.get("/by-namespace/{namespace_name}/allowed-to-user", status_code=202)
async def namespace_allowed_to_user(
    namespace_name: str,
    user: str,
    cluster: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Check whether `namespace_name` is available to `user`.

    * `202 {match: true,  vacant: false}` — namespace is held by `user`.
    * `202 {match: false, vacant: true}`  — namespace is not held by anyone (vacant).
    * `423 Locked`                        — namespace is held by a different user.

    Both 202 responses include `namespace_id` (UUID or null) and `environment_id` (UUID or null).
    The actual owner is never disclosed. Any authenticated user may ask; optional `cluster`
    disambiguates a name reused across clusters.
    """
    envs = await _env_repo.get_by_namespace(
        session, namespace_name, cluster_name=cluster
    )
    if len(envs) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"namespace '{namespace_name}' is ambiguous across clusters; specify ?cluster=",
        )
    if not envs:
        # Vacant — look up the namespace catalog to return its id.
        if cluster is not None:
            ns = await _namespace_repo.get_by_name_and_cluster(
                session, namespace_name, cluster
            )
            ns_id = ns.id if ns else None
        else:
            matches = await _namespace_repo.get_by_name(session, namespace_name)
            if len(matches) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"namespace '{namespace_name}' is ambiguous across clusters; specify ?cluster=",
                )
            ns_id = matches[0].id if matches else None
        return {
            "namespace": namespace_name,
            "namespace_id": ns_id,
            "environment_id": None,
            "user": user,
            "match": False,
            "vacant": True,
        }
    ns_booking = next(
        (b for b in envs[0].bookings if b.resource_type == ResourceType.NAMESPACE), None
    )
    if envs[0].owner_username == user:
        return {
            "namespace": namespace_name,
            "namespace_id": ns_booking.namespace_id if ns_booking else None,
            "environment_id": envs[0].id,
            "user": user,
            "match": True,
            "vacant": False,
        }
    raise HTTPException(
        status_code=423,
        detail=f"namespace '{namespace_name}' is not available to user '{user}'",
    )


@router.get("/{environment_id}")
async def get_environment(
    environment_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _env_repo.get(session, environment_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    return _serialize(env)


@router.patch("/{environment_id}/deployment-info")
async def update_environment_deployment(
    environment_id: UUID,
    body: UpdateEnvironmentDeploymentRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Replace all Jenkins deployment fields; owner, creating dispatcher or admin only."""
    try:
        env = await _update_deployment_use_case.execute(
            session, environment_id, body.to_report(), current_user
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except EnvironmentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _serialize(env)


@router.patch("/{environment_id}")
async def update_environment_name(
    environment_id: UUID,
    body: UpdateEnvironmentRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        env = await _update_name_use_case.execute(
            session, environment_id, body.name, current_user
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except EnvironmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": str(env.id), "name": env.name}


@router.put("/{environment_id}/extend")
async def extend_environment(
    environment_id: UUID,
    body: ExtendEnvironmentRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Extend the environment's shared lease — the environment and every child move to the same
    new deadline (current expiry + extend_minutes). Only READY, timed environments whose lease has
    started can be extended."""
    try:
        env = await _extend_use_case.execute(
            session, environment_id, body.extend_minutes, current_user
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except InvalidExtensionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except EnvironmentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "id": str(env.id),
        "status": _derived_status(env),
        "ttl_minutes": env.ttl_minutes,
        "expires_at": env.expires_at.isoformat(),
    }


@router.delete("/{environment_id}", status_code=202)
async def release_environment(
    environment_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Release the whole environment — tear down all its child resources together."""
    try:
        env = await _release_use_case.execute(
            session,
            environment_id,
            current_user,
            request_id=get_request_id(),
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return _serialize(env)
