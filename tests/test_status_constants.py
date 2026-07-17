"""Regression tests for P3-A′/#303: single source of truth for booking-status groups.

Verifies the two domain constants have the correct membership so that any future
BookingStatus addition that should propagate is caught at this layer.
"""
from app.domain.booking_status import LIVE_CHILD_STATUSES, LIVE_STATUSES
from app.domain.enums import BookingStatus


def test_live_statuses_excludes_released_and_failed():
    assert BookingStatus.RELEASED not in LIVE_STATUSES
    assert BookingStatus.FAILED not in LIVE_STATUSES


def test_live_statuses_includes_all_other_members():
    expected = {s for s in BookingStatus if s not in {BookingStatus.RELEASED, BookingStatus.FAILED}}
    assert LIVE_STATUSES == expected


def test_live_child_statuses_excludes_released_releasing_and_failed():
    assert BookingStatus.RELEASED not in LIVE_CHILD_STATUSES
    assert BookingStatus.RELEASING not in LIVE_CHILD_STATUSES
    assert BookingStatus.FAILED not in LIVE_CHILD_STATUSES


def test_live_child_statuses_includes_all_other_members():
    expected = {
        s for s in BookingStatus
        if s not in {BookingStatus.RELEASED, BookingStatus.RELEASING, BookingStatus.FAILED}
    }
    assert LIVE_CHILD_STATUSES == expected


def test_live_child_statuses_is_subset_of_live_statuses():
    assert LIVE_CHILD_STATUSES < LIVE_STATUSES


def test_constants_are_frozensets():
    assert isinstance(LIVE_STATUSES, frozenset)
    assert isinstance(LIVE_CHILD_STATUSES, frozenset)
