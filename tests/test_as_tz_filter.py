from app.domain.constants import PERMANENT_EXPIRES_AT
from app.presentation.templating import _as_tz


def test_as_tz_permanent_sentinel_utc_plus_does_not_raise():
    # PERMANENT_EXPIRES_AT is 9999-12-31 23:59:59 UTC; adding a UTC+ offset overflows.
    result = _as_tz(PERMANENT_EXPIRES_AT, "Europe/Moscow")
    assert result == "—"


def test_as_tz_permanent_sentinel_utc_returns_formatted():
    result = _as_tz(PERMANENT_EXPIRES_AT, "UTC")
    assert result == "9999-12-31 23:59 (UTC)"


def test_as_tz_normal_datetime_formats_correctly():
    from datetime import datetime, timezone

    dt = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = _as_tz(dt, "UTC")
    assert result == "2026-07-01 12:00 (UTC)"
