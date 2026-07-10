"""JSON API for ordering and viewing environments (a stack of child bookings)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.release_environment import EnvironmentNotFoundError
from app.presentation.routes._dispatch import resolve_owner
from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BlueprintNotFoundError, BookingPermissionError, EnvironmentItemError, NamespaceUnavailableError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation import deps

router = APIRouter(prefix="/api/environments", tags=["environments"])

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

# A child is "in flight" until it settles; an environment is FAILED if any child failed.
_IN_FLIGHT = {
    BookingStatus.QUEUED, BookingStatus.PENDING, BookingStatus.PROVISIONING,
    BookingStatus.CONFIGURING, BookingStatus.RETRY,
}


class OrderEnvironmentRequest(BaseModel):
    blueprint_name: str
    ttl_minutes: int
    # Dispatcher only: order on behalf of this user (username); the environment is owned by them.
    on_behalf_of: str | None = None
    # Order-time override of the blueprint's single namespace item (#235); both or neither.
    namespace_name: str | None = None
    cluster_name: str | None = None


def _derived_status(env: Environment) -> str:
    """Aggregate the environment's status from its children."""
    statuses = [b.status for b in env.bookings]
    if not statuses:
        return BookingStatus.READY.value
    if any(s == BookingStatus.FAILED for s in statuses):
        return BookingStatus.FAILED.value
    if any(s in _IN_FLIGHT for s in statuses):
        return BookingStatus.PROVISIONING.value
    if all(s == BookingStatus.RELEASED for s in statuses):
        return BookingStatus.RELEASED.value
    return BookingStatus.READY.value


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
            session, body.blueprint_name, body.ttl_minutes, user_id=owner_id, created_by=created_by,
            namespace_name=body.namespace_name, cluster_name=body.cluster_name,
        )
    except BlueprintNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except EnvironmentItemError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (QuotaExceededError, NamespaceUnavailableError, StaticVMUnavailableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    env.owner_username = body.on_behalf_of or current_user.username
    return _serialize(env)


@router.get("")
async def list_environments(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    if current_user.role == "admin":
        envs = await _env_repo.list_all(session)
    else:
        envs = await _env_repo.list_by_user(session, str(current_user.id))
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
    envs = await _env_repo.get_by_namespace(session, namespace_name, cluster_name=cluster)
    if not envs:
        raise HTTPException(
            status_code=404, detail=f"no active environment with namespace '{namespace_name}'",
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
    envs = await _env_repo.get_by_namespace(session, namespace_name, cluster_name=cluster)
    if len(envs) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"namespace '{namespace_name}' is ambiguous across clusters; specify ?cluster=",
        )
    if not envs:
        # Vacant — look up the namespace catalog to return its id.
        if cluster is not None:
            ns = await _namespace_repo.get_by_name_and_cluster(session, namespace_name, cluster)
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
            "namespace": namespace_name, "namespace_id": ns_id,
            "environment_id": None,
            "user": user, "match": False, "vacant": True,
        }
    ns_booking = next(
        (b for b in envs[0].bookings if b.resource_type == ResourceType.NAMESPACE), None
    )
    if envs[0].owner_username == user:
        return {
            "namespace": namespace_name, "namespace_id": ns_booking.namespace_id if ns_booking else None,
            "environment_id": envs[0].id,
            "user": user, "match": True, "vacant": False,
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
    except ValueError:
        raise HTTPException(status_code=404, detail="Environment not found")
    return _serialize(env)


@router.delete("/{environment_id}", status_code=202)
async def release_environment(
    environment_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Release the whole environment — tear down all its child resources together."""
    try:
        env = await _release_use_case.execute(session, environment_id, current_user)
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return _serialize(env)
