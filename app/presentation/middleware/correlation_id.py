"""Correlation-id middleware (#371 F-3).

Binds a `request_id` per request into the shared contextvar defined in
`app.infrastructure.logging_config` — honouring an inbound `X-Request-ID` header (e.g. from a
reverse proxy) if present, otherwise generating a fresh `uuid4`. `RequestIdFilter` (in the same
module) picks the value up from that contextvar and stamps it onto every log record emitted while
the request is in flight, so `app`'s structured JSON logs are filterable by request. The same id
is echoed back as a response header so a caller can correlate its own logs too.

`contextvars` don't cross the process boundary into Celery, so routes that dispatch a background
task read the current id via `get_request_id()` and pass it explicitly into the `TaskDispatcher`
port (see `app/application/ports.py`), which threads it into the dispatched task's arguments.
"""
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.infrastructure.logging_config import request_id_ctx_var

REQUEST_ID_HEADER = "X-Request-ID"


def get_request_id() -> str | None:
    """Return the `request_id` bound for the current context, if any.

    Used by route handlers to thread the current request's id into a `TaskDispatcher` call so a
    dispatched Celery task's own logs carry the same id.
    """
    return request_id_ctx_var.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a per-request `request_id` to a contextvar for the request's duration and echo it
    back as a response header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = request_id_ctx_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
