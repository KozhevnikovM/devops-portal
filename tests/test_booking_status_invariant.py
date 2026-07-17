"""Tests for the BookingStatus transition map + observe-only logging (#238 Phase 1)."""
import logging
from uuid import uuid4

import pytest

from app.domain.booking_status import ALLOWED_TRANSITIONS, can_transition
from app.domain.enums import BookingStatus


# ── the map ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("old, new", [
    (BookingStatus.PENDING, BookingStatus.PROVISIONING),
    (BookingStatus.PROVISIONING, BookingStatus.CONFIGURING),
    (BookingStatus.PROVISIONING, BookingStatus.RETRY),
    (BookingStatus.CONFIGURING, BookingStatus.READY),
    (BookingStatus.RETRY, BookingStatus.PROVISIONING),
    (BookingStatus.QUEUED, BookingStatus.READY),
    (BookingStatus.QUEUED, BookingStatus.RELEASED),
    (BookingStatus.READY, BookingStatus.RELEASING),
    (BookingStatus.RELEASING, BookingStatus.RELEASED),
    (BookingStatus.FAILED, BookingStatus.RELEASED),
])
def test_allowed_transitions_pass(old, new):
    assert can_transition(old, new) is True


@pytest.mark.parametrize("old, new", [
    (BookingStatus.RELEASED, BookingStatus.READY),       # terminal can't revive
    (BookingStatus.READY, BookingStatus.PROVISIONING),   # can't go backwards
    (BookingStatus.QUEUED, BookingStatus.CONFIGURING),   # pooled never configures
])
def test_disallowed_transitions_fail(old, new):
    assert can_transition(old, new) is False


def test_self_transition_is_not_a_transition():
    for s in BookingStatus:
        assert can_transition(s, s) is False


def test_released_is_terminal():
    assert ALLOWED_TRANSITIONS[BookingStatus.RELEASED] == set()


# ── enforce wiring raises on disallowed, allows legit + no-op (#238 Phase 2) ─────
def test_guard_transition_raises_on_disallowed():
    from app.infrastructure.repositories.booking_repo import _check_transition
    from app.domain.exceptions import IllegalStatusTransitionError
    with pytest.raises(IllegalStatusTransitionError):
        _check_transition("RELEASED", BookingStatus.READY, uuid4())  # terminal can't revive


def test_guard_transition_allows_legit_and_noop():
    from app.infrastructure.repositories.booking_repo import _check_transition
    _check_transition("PENDING", BookingStatus.PROVISIONING, uuid4())  # allowed → no raise
    _check_transition("READY", BookingStatus.READY, uuid4())           # no-op → no raise


def test_illegal_transition_is_a_booking_error():
    """So API routes that catch BookingError surface it as 409, not a 500."""
    from app.domain.exceptions import BookingError, IllegalStatusTransitionError
    assert issubclass(IllegalStatusTransitionError, BookingError)
