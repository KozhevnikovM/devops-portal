"""SSE endpoint pushing live booking/environment row updates (#388).

Replaces (most of) the 3s HTMX poll on `index.html`/`environments.html` with a push: one
held-open connection per open tab, subscribed to the shared Redis channel
`app.infrastructure.events.ROW_CHANGED_CHANNEL`. Each row-visible mutation re-fetches the
affected booking/environment, applies this *connection's own* `can_manage()` check, and — only
if authorized — emits the same HTML the polling endpoints already return, as a named SSE event
(`booking-<id>` / `environment-<id>`) that `sse-swap` on the row picks up.

A row a user isn't authorized to see is never rendered onto their connection — no new IDOR
surface beyond what `GET /bookings/{id}/row` and `GET /environments/{id}/row` already enforce.

Before any of that DB work, each row is pre-filtered on the routing metadata the notification
carries (#442): a row this connection's user could never manage is skipped without opening a
session. That check only ever skips work; the DB-backed `can_manage()` in the renderers stays
authoritative.
"""
import asyncio
import json
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases._permissions import can_manage
from app.domain.entities import User
from app.domain.enums import BookingStatus
from app.domain.exceptions import BookingNotFoundError, EnvironmentNotFoundError
from app.infrastructure.auth import require_user
from app.infrastructure.database.session import AsyncSessionLocal, get_async_session
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


async def _render_booking_event(current_user: User, booking_id: str) -> str | None:
    # Short-lived session for this one lookup — the SSE connection this is called from stays
    # open for as long as the tab does, so a session held for the whole connection (rather than
    # per render) pins a DB pool connection per open tab indefinitely and exhausts the pool
    # under enough concurrently-open tabs (#407).
    async with AsyncSessionLocal() as session:
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


async def _render_environment_event(current_user: User, environment_id: str) -> str | None:
    async with AsyncSessionLocal() as session:
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


# Where each row's routing lives in a row-changed payload (#442). The environment row has its own:
# an adopted namespace booking keeps its created_by, while the environment records who ordered it.
_ROUTING_KEYS: dict[str, tuple[str, str]] = {
    "booking": ("owner_id", "created_by"),
    "environment": ("environment_owner_id", "environment_created_by"),
}


def _may_concern(payload: dict, row: Literal["booking", "environment"], user: User) -> bool:
    """False only if the payload proves ``user`` cannot manage ``row`` — then no lookup is needed.

    A payload without that row's owner key (a publisher predating #442, or an environment whose
    routing couldn't be read) is "unknown", never "owned by nobody": it falls through to the
    DB-backed check like before.
    """
    owner_key, creator_key = _ROUTING_KEYS[row]
    if owner_key not in payload:
        return True
    return can_manage(owner_id=payload[owner_key], created_by=payload.get(creator_key), user=user)


def _rows_to_refresh(payload: dict) -> list[tuple[Literal["booking", "environment"], str]]:
    """Which rows a row-changed notification should re-render, booking first (#441).

    A ``progress`` notification only changes the booking's status message and provisioning log,
    which the environment row doesn't show, so it refreshes the booking row alone. Anything else
    (``lifecycle``, a missing kind from a legacy payload, or a kind this code doesn't know) also
    refreshes the parent environment row: an extra render beats a stale aggregate status.
    """
    rows: list[tuple[Literal["booking", "environment"], str]] = []
    booking_id = payload.get("booking_id")
    if booking_id:
        rows.append(("booking", booking_id))
    environment_id = payload.get("environment_id")
    if environment_id and payload.get("kind") != "progress":
        rows.append(("environment", environment_id))
    return rows


async def _event_stream(request: Request, current_user: User):
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

            for row, row_id in _rows_to_refresh(payload):
                if not _may_concern(payload, row, current_user):
                    continue
                render = _render_booking_event if row == "booking" else _render_environment_event
                chunk = await render(current_user, row_id)
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
    # `session` (from require_user's auth check) is only needed transiently, before the stream
    # starts. FastAPI doesn't release a yield-dependency's connection until the response body
    # finishes — for this StreamingResponse that's "when the tab closes," which would otherwise
    # pin a DB pool connection per open tab indefinitely (#407). Release it explicitly now;
    # each row render below opens its own short-lived session instead.
    await session.close()
    return StreamingResponse(
        _event_stream(request, current_user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx-specific: disable proxy buffering so events aren't held back (see admin-guide).
            "X-Accel-Buffering": "no",
        },
    )
