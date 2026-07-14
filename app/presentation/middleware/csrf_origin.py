"""CSRF belt-and-suspenders: reject non-safe requests whose Origin/Referer header
doesn't match BASE_URL.  SameSite=Lax is the primary defence (see docs/decisions/csrf-strategy.md).
"""
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _origin(request: Request) -> str | None:
    """Return the serialised origin from the Origin header, or from Referer as fallback."""
    if header := request.headers.get("origin"):
        return header.rstrip("/")
    if ref := request.headers.get("referer"):
        parsed = urlparse(ref)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Reject mutating requests whose Origin/Referer is present and doesn't match BASE_URL."""

    def __init__(self, app, base_url: str) -> None:
        super().__init__(app)
        self._base_url = base_url.rstrip("/")

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in _SAFE_METHODS:
            origin = _origin(request)
            if origin is not None and origin != self._base_url:
                return Response("Forbidden", status_code=403)
        return await call_next(request)
