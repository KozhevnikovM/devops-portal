"""SSE endpoint pushing live booking/environment row updates (#388).

Replaces (most of) the 3s HTMX poll on `index.html`/`environments.html` with a push: one
held-open connection per open tab, subscribed to the shared Redis channel
`app.infrastructure.events.ROW_CHANGED_CHANNEL`. Each row-visible mutation re-fetches the
affected booking/environment, applies this *connection's own* `can_manage()` check, and — only
if authorized — emits the same HTML the polling endpoints already return, as a named SSE event
(`booking-<id>` / `environment-<id>`) that `sse-swap` on the row picks up.

A row a user isn't authorized to see is never rendered onto their connection — no new IDOR
surface beyond what `GET /bookings/{id}/row` and `GET /environments/{id}/row` already enforce.
"""
import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases._permissions import can_manage
from app.domain.entities import User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError, EnvironmentNotFoundError
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import get_async_session
from app.infrastructure.events import ROW_CHANGED_CHANNEL, get_async_redis
from app.presentation import deps
from app.presentation.routes.environments import _annotate
from app.presentation.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_booking_repo = deps.booking_repo
_env_repo = deps.env_repo

# How long get_message() blocks waiting for a pub/sub message before we send an SSE comment as a
# keepalive — without one, an idle reverse proxy (or the browser itself) can silently drop a
# connection that never seems to send anything.
_HEARTBEAT_INTERVAL = 15


def _sse_event(event: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in html.splitlines())
    return f"event: {event}\n{data}\n\n"


async def _render_booking_event(session: AsyncSession, current_user: User, booking_id: str) -> str | None:
    try:
        booking = await _booking_repo.get(session, UUID(booking_id))
    except (BookingNotFoundError, ValueError):
        return None
    if not can_manage(owner_id=booking.user_id, created_by=booking.created_by, user=current_user):
        return None
    if booking.status == BookingStatus.QUEUED:
        booking.queue_position = await _booking_repo.queue_position(
            session, booking.resource_type.value, booking.created_at
        )
    html = templates.get_template("partials/booking_row.html").render(
        booking=booking, current_user=current_user,
    )
    return _sse_event(f"booking-{booking_id}", html)


async def _render_environment_event(session: AsyncSession, current_user: User, environment_id: str) -> str | None:
    try:
        env = await _env_repo.get(session, UUID(environment_id))
    except (EnvironmentNotFoundError, ValueError):
        return None
    if not can_manage(owner_id=env.user_id, created_by=env.created_by, user=current_user):
        return None
    html = templates.get_template("partials/environment_row.html").render(
        environment=_annotate(env), current_user=current_user,
    )
    return _sse_event(f"environment-{environment_id}", html)


async def _event_stream(request: Request, session: AsyncSession, current_user: User):
    redis_client = get_async_redis()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(ROW_CHANGED_CHANNEL)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_HEARTBEAT_INTERVAL
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("events stream: pub/sub read failed, ending connection")
                break
            if message is None:
                yield ": keepalive\n\n"  # SSE comment — no event, just keeps the connection warm
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue

            booking_id = payload.get("booking_id")
            if booking_id:
                chunk = await _render_booking_event(session, current_user, booking_id)
                if chunk:
                    yield chunk

            environment_id = payload.get("environment_id")
            if environment_id:
                chunk = await _render_environment_event(session, current_user, environment_id)
                if chunk:
                    yield chunk
    finally:
        await pubsub.unsubscribe(ROW_CHANGED_CHANNEL)
        await pubsub.aclose()


@router.get("/events/stream")
async def events_stream(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_user),
):
    return StreamingResponse(
        _event_stream(request, session, current_user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx-specific: disable proxy buffering so events aren't held back (see admin-guide).
            "X-Accel-Buffering": "no",
        },
    )
