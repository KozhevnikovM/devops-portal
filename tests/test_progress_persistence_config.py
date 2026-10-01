"""Constants and settings behind progress persistence batching (#444)."""
import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.booking_status import (
    PROVISIONING_PROGRESS_STATUSES,
    TEARDOWN_PROGRESS_STATUSES,
)
from app.domain.constants import PROVISIONING_LOG_MAX_CHARS
from app.domain.enums import BookingStatus

_DOMAIN = Path(__file__).parent.parent / "app" / "domain"
_FRAMEWORKS = ("sqlalchemy", "fastapi", "celery", "starlette", "redis")


def test_log_cap_is_50000():
    assert PROVISIONING_LOG_MAX_CHARS == 50_000


def test_progress_accepting_statuses():
    assert PROVISIONING_PROGRESS_STATUSES == {BookingStatus.PROVISIONING, BookingStatus.CONFIGURING}
    assert TEARDOWN_PROGRESS_STATUSES == {BookingStatus.RELEASING}


@pytest.mark.parametrize("module", ["constants.py", "booking_status.py"])
def test_domain_modules_import_no_frameworks(module):
    tree = ast.parse((_DOMAIN / module).read_text())
    imported = [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    ] + [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [m for m in imported if m.split(".")[0] in _FRAMEWORKS]


def test_flush_settings_defaults():
    s = Settings()
    assert s.PROGRESS_FLUSH_INTERVAL_MS == 500
    assert s.PROGRESS_FLUSH_MESSAGE_THRESHOLD == 50
    assert s.PROGRESS_FLUSH_CHAR_THRESHOLD == 16_384


def test_flush_interval_zero_is_allowed():
    assert Settings(PROGRESS_FLUSH_INTERVAL_MS=0).PROGRESS_FLUSH_INTERVAL_MS == 0


@pytest.mark.parametrize("field,bad", [
    ("PROGRESS_FLUSH_INTERVAL_MS", -1),
    ("PROGRESS_FLUSH_MESSAGE_THRESHOLD", 0),
    ("PROGRESS_FLUSH_MESSAGE_THRESHOLD", -5),
    ("PROGRESS_FLUSH_CHAR_THRESHOLD", 0),
    ("PROGRESS_FLUSH_CHAR_THRESHOLD", -5),
])
def test_flush_settings_reject_invalid(field, bad):
    with pytest.raises(ValidationError):
        Settings(**{field: bad})
