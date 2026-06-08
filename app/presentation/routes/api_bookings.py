"""JSON-only programmatic booking API under /api/bookings.

The browser's HTMX routes live in `bookings.py` and return HTML fragments; this router is the
canonical API surface for clients (Jenkins/CI). Both share the same application use cases so the
two never drift.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.book_namespace import BookNamespaceUseCase
from app.application.use_cases.create_booking import CreateBookingUseCase
from app.application.use_cases.extend_booking import ExtendBookingUseCase
from app.application.use_cases.release_booking import ReleaseBookingUseCase
from app.application.use_cases.reserve_static_vm import ReserveStaticVMUseCase
from app.domain.entities import Booking, User
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import (
    BookingError, BookingNotFoundError, BookingPermissionError, NamespaceUnavailableError,
    QuotaExceededError, StaticVMUnavailableError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.celery_dispatcher import CeleryTaskDispatcher
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.infrastructure.repositories.hw_config_repo import HWConfigRepository
from app.infrastructure.repositories.image_repo import ImageRepository
from app.infrastructure.repositories.namespace_repo import NamespaceRepository
from app.infrastructure.repositories.static_vm_repo import StaticVMRepository

router = APIRouter(prefix="/api/bookings", tags=["bookings"])

_repo = BookingRepository()
_image_repo = ImageRepository()
_hw_config_repo = HWConfigRepository()
_namespace_repo = NamespaceRepository()
_static_vm_repo = StaticVMRepository()
_dispatcher = CeleryTaskDispatcher()
_create_use_case = CreateBookingUseCase(_repo, _image_repo, _hw_config_repo, dispatcher=_dispatcher)
_extend_use_case = ExtendBookingUseCase(_repo)
_release_use_case = ReleaseBookingUseCase(_repo, _dispatcher)
_book_namespace_use_case = BookNamespaceUseCase(_repo, _namespace_repo)
_reserve_static_vm_use_case = ReserveStaticVMUseCase(_repo, _static_vm_repo)


class CreateBookingRequest(BaseModel):
    resource_type: str = ResourceType.VM.value
    ttl_minutes: int
    image_id: UUID | None = None
    hw_config_id: UUID | None = None
    # Order a VM by catalog names instead of ids (an explicit *_id wins). Names are unique.
    image_name: str | None = None
    hw_config_name: str | None = None
    namespace_id: UUID | None = None
    # Order a specific namespace by its (name, cluster) pair instead of namespace_id.
    namespace_name: str | None = None
    cluster_name: str | None = None
    static_vm_id: UUID | None = None
    # Order a specific static VM by name instead of static_vm_id.
    static_vm_name: str | None = None


class ExtendBookingRequest(BaseModel):
    extend_minutes: int


# ── Serialization ──────────────────────────────────────────────────────────────
def _summary(b: Booking) -> dict:
    """Full booking view used by the list endpoint. Never includes secrets."""
    return {
        "id": str(b.id),
        "user_id": b.user_id,
        "status": b.status.value,
        "resource_type": b.resource_type.value,
        "ttl_minutes": b.ttl_minutes,
        "expires_at": b.expires_at.isoformat(),
        "created_at": b.created_at.isoformat(),
        "image_id": str(b.image_id) if b.image_id else None,
        "image_name": b.image_name,
        "hw_config_id": str(b.hw_config_id) if b.hw_config_id else None,
        "hw_config_name": b.hw_config_name,
        "vm_ip": b.vm_ip,
        "namespace": b.namespace_name,
        "cluster": b.cluster_name,
        "api_url": b.api_url,
        "static_vm": b.static_vm_name,
        "host": b.static_vm_host,
        "username": b.static_vm_username,
    }


def _created(b: Booking) -> dict:
    """Owner-scoped creation view — resource-type specific, includes one-time secrets."""
    base = {
        "id": str(b.id),
        "status": b.status.value,
        "resource_type": b.resource_type.value,
        "ttl_minutes": b.ttl_minutes,
        "expires_at": b.expires_at.isoformat(),
        "created_at": b.created_at.isoformat(),
    }
    if b.resource_type == ResourceType.NAMESPACE:
        base.update({
            "namespace": b.namespace_name,
            "cluster": b.cluster_name,
            "api_url": b.api_url,
            "queue_position": b.queue_position,
        })
    elif b.resource_type == ResourceType.STATIC_VM:
        base.update({
            "static_vm": b.static_vm_name,
            "host": b.static_vm_host,
            "username": b.static_vm_username,
            "password": b.static_vm_password,
            "ssh_key": b.static_vm_ssh_key,
            "queue_position": b.queue_position,
        })
    else:  # VM
        base.update({
            "image_id": str(b.image_id),
            "image_name": b.image_name,
            "hw_config_id": str(b.hw_config_id),
            "hw_config_name": b.hw_config_name,
        })
    return base


async def _attach_queue_position(session: AsyncSession, booking: Booking) -> None:
    if booking.status == BookingStatus.QUEUED:
        booking.queue_position = await _repo.queue_position(
            session, booking.resource_type.value, booking.created_at
        )


async def _resolve_catalog_id(session, id_, name, get_by_name, label):
    """Resolve a catalog entry to its id: an explicit id wins; else look up by name.

    Returns None when neither id nor name is given (the caller reports the 400). Raises 400 if a
    name is given but matches no active catalog entry.
    """
    if id_ is not None:
        return id_
    if name:
        entry = await get_by_name(session, name)
        if entry is None:
            raise HTTPException(status_code=400, detail=f"no {label} named '{name}'")
        return entry.id
    return None


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.get("")
async def list_bookings(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # Owner-scoped: non-admins see only their own bookings; admins see all. Secrets are never
    # vended here — only on the owner-scoped creation response.
    if current_user.role == "admin":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
    return [_summary(b) for b in bookings]


@router.post("", status_code=201)
async def create_booking(
    body: CreateBookingRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    # ── Namespace — reserve from the pool (pick-specific or any), else queue ──
    if body.resource_type == ResourceType.NAMESPACE.value:
        # A (name, cluster) pair identifies a namespace; both must be given together.
        if bool(body.namespace_name) != bool(body.cluster_name):
            raise HTTPException(
                status_code=400,
                detail="namespace_name and cluster_name must be provided together",
            )
        try:
            booking = await _book_namespace_use_case.execute(
                session, body.ttl_minutes, user_id=str(current_user.id),
                namespace_id=body.namespace_id,
                namespace_name=body.namespace_name,
                cluster_name=body.cluster_name,
            )
        except NamespaceUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # ── Static VM — reserve from the pool, no provisioning ──
    elif body.resource_type == ResourceType.STATIC_VM.value:
        static_vm_id = body.static_vm_id
        if static_vm_id is None and body.static_vm_name:
            static_vm = await _static_vm_repo.get_by_name(session, body.static_vm_name)
            if static_vm is None:
                raise HTTPException(status_code=400, detail=f"no static VM named '{body.static_vm_name}'")
            static_vm_id = static_vm.id
        try:
            booking = await _reserve_static_vm_use_case.execute(
                session, body.ttl_minutes, user_id=str(current_user.id), static_vm_id=static_vm_id
            )
        except StaticVMUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # ── VM — provisioning flow ──
    else:
        image_id = await _resolve_catalog_id(
            session, body.image_id, body.image_name, _image_repo.get_by_name, "VM image",
        )
        hw_config_id = await _resolve_catalog_id(
            session, body.hw_config_id, body.hw_config_name, _hw_config_repo.get_by_name, "hardware config",
        )
        if image_id is None or hw_config_id is None:
            raise HTTPException(
                status_code=400,
                detail="image (id or name) and hardware config (id or name) are required",
            )
        try:
            booking = await _create_use_case.execute(
                session, body.ttl_minutes, image_id, hw_config_id, user_id=str(current_user.id)
            )
        except QuotaExceededError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    booking.owner_username = current_user.username
    await _attach_queue_position(session, booking)
    return _created(booking)


@router.delete("/{booking_id}", status_code=202)
async def release_booking(
    booking_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _release_use_case.execute(session, booking_id, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"id": str(booking.id), "status": booking.status.value}


@router.put("/{booking_id}/extend")
async def extend_booking(
    booking_id: UUID,
    body: ExtendBookingRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _extend_use_case.execute(session, booking_id, body.extend_minutes, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except BookingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "id": str(booking.id),
        "status": booking.status.value,
        "ttl_minutes": booking.ttl_minutes,
        "expires_at": booking.expires_at.isoformat(),
    }


@router.get("/{booking_id}/audit")
async def get_booking_audit(
    booking_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    try:
        booking = await _repo.get(session, booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.user_id != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not the booking owner")

    entries = await _repo.list_audit(session, booking_id)
    return [
        {
            "id": str(e.id),
            "booking_id": str(e.booking_id),
            "action": e.action,
            "old_status": e.old_status,
            "new_status": e.new_status,
            "actor_id": e.actor_id,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
