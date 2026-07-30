"""Tests for `app/infrastructure/logging_config.py` (#371 F-1/F-2/F-3).

Covers:
- `configure_logging()` installs a JSON formatter on the root logger with the expected keys.
- `RequestIdFilter` injects the current `request_id_ctx_var` value into log records, and leaves
  it out entirely when nothing is bound.
- `JsonFormatter` surfaces a captured traceback (`exc_info`) under `exception` — this is what
  makes F-5's `logger.error` → `logger.exception` change actually visible in the log output.
"""
import json
import logging

from app.infrastructure.logging_config import (
    JsonFormatter, RequestIdFilter, configure_logging, request_id_ctx_var,
)


def _make_record(logger_name="test.logger", msg="hello", level=logging.INFO, exc_info=None):
    return logging.LogRecord(
        name=logger_name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


def test_json_formatter_has_expected_keys():
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["timestamp"]
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello"


def test_json_formatter_output_is_valid_json():
    record = _make_record(msg="a %s message", level=logging.WARNING)
    record.args = ("formatted",)
    line = JsonFormatter().format(record)
    payload = json.loads(line)  # raises if not valid JSON
    assert payload["message"] == "a formatted message"
    assert payload["level"] == "WARNING"


def test_json_formatter_captures_exception_traceback():
    """F-5's fix (logger.exception instead of logger.error) only matters if the formatter
    actually surfaces the traceback it captures."""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _make_record(exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "exception" in payload
    assert "ValueError: boom" in payload["exception"]


def test_json_formatter_includes_extra_fields():
    record = _make_record()
    record.request_id = "abc-123"
    record.booking_id = "booking-1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "abc-123"
    assert payload["booking_id"] == "booking-1"


def test_request_id_filter_injects_bound_request_id():
    token = request_id_ctx_var.set("req-42")
    try:
        record = _make_record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-42"
    finally:
        request_id_ctx_var.reset(token)


def test_request_id_filter_leaves_record_untouched_when_unbound():
    assert request_id_ctx_var.get() is None
    record = _make_record()
    RequestIdFilter().filter(record)
    assert not hasattr(record, "request_id")


def test_request_id_flows_through_configured_root_logger(capsys):
    """End-to-end: configure_logging() + a bound request_id together produce a JSON log line
    with request_id in it — the exact shape production log lines take."""
    configure_logging()
    logger = logging.getLogger("test.e2e.logging_config")
    token = request_id_ctx_var.set("req-e2e-1")
    try:
        logger.info("something happened")
    finally:
        request_id_ctx_var.reset(token)

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "expected at least one JSON log line on stdout"
    payload = json.loads(lines[-1])
    assert payload["message"] == "something happened"
    assert payload["logger"] == "test.e2e.logging_config"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-e2e-1"


def test_configure_logging_is_idempotent():
    """Calling configure_logging() twice must not double-emit (duplicate handlers)."""
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
