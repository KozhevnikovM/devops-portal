"""Domain rules for extending an environment's shared lease (#532)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.constants import MAX_TTL_MINUTES, PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.domain.exceptions import EnvironmentError, InvalidExtensionError
from app.domain.lease import Lease, environment_extend_blocker

_T = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# ── Lease.extended_within_term_by ───────────────────────────────────────────────
def test_extends_from_current_deadline_not_now():
    lease = Lease(60, _T)
    assert lease.extended_within_term_by(720) == Lease(780, _T + timedelta(minutes=720))


@pytest.mark.parametrize("minutes", [0, -30])
def test_nonpositive_increment_is_rejected(minutes):
    with pytest.raises(InvalidExtensionError, match="positive"):
        Lease(60, _T).extended_within_term_by(minutes)


def test_invalid_extension_is_an_environment_error():
    assert issubclass(InvalidExtensionError, EnvironmentError)


def test_reaching_the_permanent_sentinel_is_rejected():
    minutes_to_sentinel = int((PERMANENT_EXPIRES_AT - _T).total_seconds() // 60) + 1
    with pytest.raises(InvalidExtensionError, match="too large"):
        Lease(60, _T).extended_within_term_by(minutes_to_sentinel)


def test_past_datetime_max_is_rejected_not_overflowed():
    with pytest.raises(InvalidExtensionError, match="too large"):
        Lease(60, _T).extended_within_term_by(10**15)


def test_ttl_beyond_the_column_limit_is_rejected():
    with pytest.raises(InvalidExtensionError, match="too large"):
        Lease(60, _T).extended_within_term_by(MAX_TTL_MINUTES)


def test_extended_by_keeps_its_forever_semantics():
    # The standalone booking "Forever" option relies on this; environment extension must not.
    assert Lease(60, _T).extended_by(0) == Lease(0, PERMANENT_EXPIRES_AT)


# ── environment_extend_blocker ──────────────────────────────────────────────────
def test_ready_started_timed_environment_is_eligible():
    assert environment_extend_blocker(BookingStatus.READY, Lease(60, _T)) is None


def test_permanent_environment_is_blocked():
    reason = environment_extend_blocker(
        BookingStatus.READY, Lease(0, PERMANENT_EXPIRES_AT)
    )
    assert reason is not None and "permanent" in reason


@pytest.mark.parametrize(
    "status",
    [BookingStatus.PROVISIONING, BookingStatus.FAILED, BookingStatus.RELEASED],
)
def test_non_ready_environment_is_blocked(status):
    reason = environment_extend_blocker(status, Lease(60, _T))
    assert reason is not None and "READY" in reason


def test_lease_not_started_is_blocked():
    reason = environment_extend_blocker(BookingStatus.READY, Lease.pending(60))
    assert reason is not None and "not started" in reason
