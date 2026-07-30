"""Tests for `CorrelationIdMiddleware` (#371 F-3).

Covers:
- A request_id is generated when the client doesn't supply one, and echoed back as a response
  header.
- An inbound `X-Request-ID` header is honored (not overwritten).
- The contextvar is unbound again once the request completes (no leakage across requests).
- A log line emitted while handling the request carries the same `request_id` (via
  `RequestIdFilter` + `JsonFormatter`, see `app/infrastructure/logging_config.py`).
"""
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.infrastructure.logging_config import configure_logging, request_id_ctx_var
from app.presentation.middleware.correlation_id import (
    REQUEST_ID_HEADER, CorrelationIdMiddleware, get_request_id,
)

_route_logger = logging.getLogger("tests.test_correlation_id")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/ping")
    async def ping():
        return {"request_id": get_request_id()}

    @app.get("/log")
    async def log_route():
        _route_logger.info("handling request")
        return {"ok": True}

    return app


def _client() -> TestClient:
    return TestClient(_build_app())


def test_generates_request_id_when_absent():
    resp = _client().get("/ping")
    assert resp.status_code == 200
    assert REQUEST_ID_HEADER in resp.headers
    # The id echoed back matches what the route saw via the contextvar mid-request.
    assert resp.headers[REQUEST_ID_HEADER] == resp.json()["request_id"]


def test_honors_inbound_request_id():
    resp = _client().get("/ping", headers={REQUEST_ID_HEADER: "client-supplied-id"})
    assert resp.headers[REQUEST_ID_HEADER] == "client-supplied-id"
    assert resp.json()["request_id"] == "client-supplied-id"


def test_generated_ids_differ_across_requests():
    client = _client()
    first = client.get("/ping").headers[REQUEST_ID_HEADER]
    second = client.get("/ping").headers[REQUEST_ID_HEADER]
    assert first != second


def test_request_id_unbound_after_request_completes():
    assert request_id_ctx_var.get() is None
    _client().get("/ping")
    assert request_id_ctx_var.get() is None


def test_log_line_during_request_carries_request_id(capsys):
    """A downstream logger.info(...) call made while handling the request is stamped with the
    same request_id that gets echoed back on the response — the whole point of F-3."""
    configure_logging()
    resp = _client().get("/log", headers={REQUEST_ID_HEADER: "req-fixed-id"})
    assert resp.status_code == 200
    assert resp.headers[REQUEST_ID_HEADER] == "req-fixed-id"

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    matching = [json.loads(line) for line in lines if "handling request" in line]
    assert matching, f"expected a 'handling request' log line, got: {lines}"
    assert matching[-1]["request_id"] == "req-fixed-id"
