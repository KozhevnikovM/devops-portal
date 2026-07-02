"""JSON API routes for namespaces and namespace sharing.

Endpoints:
  GET    /api/namespaces                                 — list namespaces (all authenticated users)
  POST   /api/bookings/{booking_id}/shares              — share with a user (owner/admin)
  GET    /api/bookings/{booking_id}/shares              — list shares (owner/admin)
  DELETE /api/bookings/{booking_id}/shares/{username}   — revoke (owner/admin)
  GET    /api/namespaces/shared-with-me                 — namespaces shared with the caller
"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import User
from app.domain.exceptions import (
    BookingNotFoundError,
    BookingPermissionError,
    NamespaceShareDuplicateError,
    NamespaceShareNotFoundError,
    NamespaceShareSelfError,
    NamespaceShareUserNotFoundError,
)
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.presentation import deps

router = APIRouter(tags=["namespaces"])

_share_uc = deps.share_namespace_uc
_revoke_uc = deps.revoke_namespace_share_uc
_share_repo = deps.namespace_share_repo
_booking_repo = deps.booking_repo
_namespace_repo = deps.namespace_repo


@router.get("/api/namespaces")
async def list_namespaces(
    filter: Literal["all", "active", "available"] = Query("all"),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Return namespaces from the pool.

    - ``all`` (default) — every namespace including inactive ones
    - ``active`` — active namespaces (inactive excluded)
    - ``available`` — active namespaces not currently held by any booking
    """
    if filter == "available":
        namespaces = await _namespace_repo.list_available(session)
    elif filter == "active":
        namespaces = await _namespace_repo.list_active(session)
    else:
        namespaces = await _namespace_repo.list_all(session)

    return [
        {
            "id": str(ns.id),
            "name": ns.name,
            "cluster_name": ns.cluster_name,
            "api_url": ns.api_url,
            "is_active": ns.is_active,
        }
        for ns in namespaces
    ]


class ShareRequest(BaseModel):
    username: str


@router.post("/api/bookings/{booking_id}/shares", status_code=201)
async def create_share(
    booking_id: UUID,
    body: ShareRequest,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Share a namespace booking with another portal user (read-only)."""
    try:
        share = await _share_uc.execute(session, booking_id, body.username, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NamespaceShareSelfError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NamespaceShareUserNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NamespaceShareDuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {
        "booking_id": str(share.booking_id),
        "shared_with": share.shared_with_username,
        "created_at": share.created_at.isoformat(),
    }


@router.get("/api/bookings/{booking_id}/shares")
async def list_shares(
    booking_id: UUID,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """List all users this namespace booking is shared with (owner/admin only)."""
    from app.application.use_cases._permissions import can_manage  # noqa: PLC0415

    try:
        booking = await _booking_repo.get(session, booking_id)
    except (ValueError, BookingNotFoundError):
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not authorized to view shares for this booking")

    shares = await _share_repo.list_by_booking(session, booking_id)
    return [
        {
            "username": s.shared_with_username,
            "created_at": s.created_at.isoformat(),
        }
        for s in shares
    ]


@router.delete("/api/bookings/{booking_id}/shares/{username}", status_code=204)
async def revoke_share(
    booking_id: UUID,
    username: str,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Revoke a namespace share (owner/admin only)."""
    try:
        await _revoke_uc.execute(session, booking_id, username, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except NamespaceShareUserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NamespaceShareNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/namespaces/shared-with-me")
async def shared_with_me(
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Return namespace bookings currently shared with the calling user."""
    entries = await _share_repo.list_shared_with_user(session, current_user.id)
    return entries
