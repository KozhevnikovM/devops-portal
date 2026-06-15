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


# ── observe-only wiring logs (never raises) ──────────────────────────────────────
def test_observe_transition_warns_on_disallowed(caplog):
    from app.infrastructure.repositories.booking_repo import _observe_transition
    bid = uuid4()
    with caplog.at_level(logging.WARNING):
        _observe_transition("RELEASED", BookingStatus.READY, bid)  # disallowed → warns, no raise
    assert any("Disallowed booking status transition" in r.message for r in caplog.records)


def test_observe_transition_silent_on_allowed_and_noop(caplog):
    from app.infrastructure.repositories.booking_repo import _observe_transition
    bid = uuid4()
    with caplog.at_level(logging.WARNING):
        _observe_transition("PENDING", BookingStatus.PROVISIONING, bid)  # allowed
        _observe_transition("READY", BookingStatus.READY, bid)           # no-op
    assert caplog.records == []
