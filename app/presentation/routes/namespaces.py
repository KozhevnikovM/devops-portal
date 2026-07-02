"""Browser (HTMX) routes for namespace sharing.

Serves the share-management panel for a specific booking and the "Shared with me" section.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases._permissions import can_manage
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
from app.presentation.templating import templates

router = APIRouter()

_share_uc = deps.share_namespace_uc
_revoke_uc = deps.revoke_namespace_share_uc
_share_repo = deps.namespace_share_repo
_booking_repo = deps.booking_repo


def _share_panel_ctx(booking_id: UUID, shares, current_user: User, error: str | None = None):
    return {
        "booking_id": booking_id,
        "shares": shares,
        "current_user": current_user,
        "error": error,
    }


@router.get("/namespaces/{booking_id}/shares", response_class=HTMLResponse)
async def get_share_panel(
    booking_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Return the share-management panel for a namespace booking (HTMX)."""
    try:
        booking = await _booking_repo.get(session, booking_id)
    except (ValueError, BookingNotFoundError):
        raise HTTPException(status_code=404, detail="Booking not found")

    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    shares = await _share_repo.list_by_booking(session, booking_id)
    return templates.TemplateResponse(
        request, "partials/namespace_share_panel.html",
        _share_panel_ctx(booking_id, shares, current_user),
    )


@router.post("/namespaces/{booking_id}/shares", response_class=HTMLResponse)
async def add_share(
    booking_id: UUID,
    request: Request,
    username: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Add a share via form submit (HTMX POST), returns updated panel."""
    error: str | None = None
    try:
        await _share_uc.execute(session, booking_id, username, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (NamespaceShareSelfError, NamespaceShareUserNotFoundError, ValueError) as exc:
        error = str(exc)
    except NamespaceShareDuplicateError as exc:
        error = str(exc)

    shares = await _share_repo.list_by_booking(session, booking_id)
    return templates.TemplateResponse(
        request, "partials/namespace_share_panel.html",
        _share_panel_ctx(booking_id, shares, current_user, error),
    )


@router.delete("/namespaces/{booking_id}/shares/{username}", response_class=HTMLResponse)
async def revoke_share(
    booking_id: UUID,
    username: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Revoke a share via HTMX DELETE, returns updated panel."""
    try:
        await _revoke_uc.execute(session, booking_id, username, current_user)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found")
    except BookingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (NamespaceShareUserNotFoundError, NamespaceShareNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    shares = await _share_repo.list_by_booking(session, booking_id)
    return templates.TemplateResponse(
        request, "partials/namespace_share_panel.html",
        _share_panel_ctx(booking_id, shares, current_user),
    )


@router.get("/namespaces/shared-with-me", response_class=HTMLResponse)
async def shared_with_me(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    """Return the 'Shared with me' section (HTMX or direct navigation)."""
    entries = await _share_repo.list_shared_with_user(session, current_user.id)
    return templates.TemplateResponse(
        request, "partials/shared_namespaces_section.html",
        {"entries": entries, "current_user": current_user},
    )
