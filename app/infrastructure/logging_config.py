"""Unified logging configuration shared by the `app` (FastAPI/uvicorn), `worker`, and `beat`
processes (#371 F-1/F-2/F-3).

Before this module existed, the FastAPI process never called `logging.basicConfig`/`dictConfig`
at all — the root logger sat at its default `WARNING` level with no handler, so every
`logger.info(...)` call reachable from a request was silently dropped. Celery's own
`worker_hijack_root_logger` (default `True`) meanwhile installed a *different* format for
`worker`/`beat`. `configure_logging()` is the single place that installs the root logger's
handler/formatter/filter, so all three processes end up with byte-for-byte the same JSON shape.

Call it once, before anything else logs:
  - `app/main.py`, before the FastAPI app is constructed.
  - `app/infrastructure/celery_app.py`, via Celery's `setup_logging` signal (with
    `worker_hijack_root_logger=False` set on the Celery app so Celery doesn't install its own
    conflicting config afterwards).
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

# Bound by `app.presentation.middleware.correlation_id.CorrelationIdMiddleware` for the duration
# of an HTTP request, and by each Celery task (`app/tasks/provision.py`, `app/tasks/teardown.py`)
# for the duration of its own run — a single shared contextvar so `RequestIdFilter` below works
# identically in both the `app` and `worker` processes. `contextvars.ContextVar`s do not cross
# process boundaries, which is exactly why the request_id is also passed as an explicit argument
# through `TaskDispatcher` (see `app/application/ports.py`) rather than relied on implicitly.
request_id_ctx_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# Attribute names present on every stdlib `LogRecord` (plus `message`/`asctime`, which
# `Formatter.format()`/`formatMessage()` add). Anything else found on a record — via
# `logger.info(..., extra={...})` or a `logging.Filter` like `RequestIdFilter` — is "extra" and
# gets folded into the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "message", "asctime",
    "taskName",  # asyncio task name, stamped by logging on 3.12+
})


class JsonFormatter(logging.Formatter):
    """Stdlib-only structured JSON formatter (#371 F-2) — no new dependency.

    Every line is a single `json.dumps` object with `timestamp`, `level`, `logger`, `message`,
    plus whatever extra fields are present on the record (e.g. `request_id` via
    `RequestIdFilter`, or anything passed as `extra={...}` at the call site). A record with
    `exc_info` (e.g. from `logger.exception(...)`) gets its formatted traceback under
    `exception` — this is what makes F-5's `logger.error` → `logger.exception` fix actually
    surface the traceback instead of losing it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


class RequestIdFilter(logging.Filter):
    """Injects the current `request_id` (if any is bound in `request_id_ctx_var`) into every
    log record's extras, so it flows through to `JsonFormatter`'s output (#371 F-3)."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = request_id_ctx_var.get()
        if request_id is not None:
            record.request_id = request_id
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Install one root-logger handler/formatter/filter shared by `app`, `worker`, and `beat`.

    Idempotent — clears any existing root handlers first, so calling it more than once (e.g. a
    re-entrant Celery `setup_logging` signal, or repeated calls in tests) never double-emits.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
