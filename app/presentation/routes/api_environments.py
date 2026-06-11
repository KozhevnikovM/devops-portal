"""JSON API for ordering and viewing environments (a stack of child bookings)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.application.use_cases.order_environment import OrderEnvironmentUseCase
from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.application.use_cases.release_environment import (
    EnvironmentNotFoundError, ReleaseEnvironmentUseCase,
)
from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase
from app.application.use_cases._permissions import can_manage
from app.presentation.routes._dispatch import resolve_owner
from app.domain.entities import Environment, User
from app.domain.enums import BookingStatus
from app.domain.exceptions import (
    BlueprintNotFoundError, BookingPermissionError, EnvironmentItemError, NamespaceUnavailableError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.celery_dispatcher import CeleryTaskDispatcher
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.environment_blueprint_repo import EnvironmentBlueprintRepository
from app.infrastructure.repositories.environment_repo import EnvironmentRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.role_repo import RoleRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

router = APIRouter(prefix="/api/environments", tags=["environments"])

_repo = BookingRepository()
_env_repo = EnvironmentRepository()
_blueprint_repo = EnvironmentBlueprintRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_role_repo = RoleRepository()
_namespace_repo = NamespaceRepository()
_static_vm_repo = StaticVMRepository()
_dispatcher = CeleryTaskDispatcher()
_create_use_case = CreateBookingUseCase(_repo, _image_repo, _hw_config_repo, dispatcher=_dispatcher)
_reserve_static_vm_use_case = ReserveStaticVMUseCase(_repo, _static_vm_repo)
_book_namespace_use_case = BookNamespaceUseCase(_repo, _namespace_repo)
_order_use_case = OrderEnvironmentUseCase(
    _env_repo, _blueprint_repo, _repo, _create_use_case, _reserve_static_vm_use_case,
    _book_namespace_use_case, _image_repo, _hw_config_repo, _role_repo, _static_vm_repo, _dispatcher,
)
_release_booking_use_case = ReleaseBookingUseCase(_repo, _dispatcher)
_release_use_case = ReleaseEnvironmentUseCase(_env_repo, _release_booking_use_case)

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
    try:
        env = await _order_use_case.execute(
            session, body.blueprint_name, body.ttl_minutes, user_id=owner_id, created_by=created_by,
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

    Lets a pipeline find the stack it owns by namespace instead of environment id. Optional `cluster`
    disambiguates a name reused across clusters. Owned/dispatched/admin → 200; owned by someone else
    → 409 (the other owner is not disclosed); not found / free / standalone namespace → 404.
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
    env = envs[0]
    if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
        raise HTTPException(
            status_code=409,
            detail=f"namespace '{namespace_name}' is in use by another user's environment",
        )
    return _serialize(env)


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
    if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not the environment owner")
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
