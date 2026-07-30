"""Tests for the Celery-side half of #371 F-1 (`app/infrastructure/celery_app.py`).

Celery's `worker_hijack_root_logger` defaults to `True`, which installs its own
`[%(asctime)s: %(levelname)s/%(processName)s] %(message)s` root-logger config — a different
format than the `app` process ever got (which, before this feature, got none at all). Disabling
it and wiring `configure_logging()` onto the `setup_logging` signal is what makes `worker`/`beat`
end up with the exact same JSON formatter as `app`.
"""
from unittest.mock import patch

from app.infrastructure.celery_app import _configure_worker_logging, celery_app


def test_worker_hijack_root_logger_disabled():
    assert celery_app.conf.worker_hijack_root_logger is False


def test_setup_logging_signal_calls_configure_logging():
    with patch("app.infrastructure.celery_app.configure_logging") as mock_configure:
        _configure_worker_logging()
    mock_configure.assert_called_once()
