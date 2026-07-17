"""Regression tests for P1-A/#305: status transition enforcement on the Booking aggregate.

Covers:
- Booking.transition_to() raises IllegalStatusTransitionError on disallowed moves
- Booking.transition_to() succeeds for every allowed transition
- Booking.transition_to() is a no-op for old == new (idempotent re-write)
- _to_entity raises a clear ValueError on an unrecognised stored status (I7)
- _assign_resource_and_ready raises IllegalStatusTransitionError when the booking
  is not QUEUED (regression for D2)
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.domain.exceptions import IllegalStatusTransitionError


def _make_booking(status: BookingStatus) -> Booking:
    return Booking(
        id=uuid4(),
        user_id=str(uuid4()),
        status=status,
        ttl_minutes=60,
        expires_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )


# ── Booking.transition_to() ────────────────────────────────────────────────────

def test_transition_to_raises_on_disallowed_move():
    booking = _make_booking(BookingStatus.RELEASED)
    with pytest.raises(IllegalStatusTransitionError):
        booking.transition_to(BookingStatus.READY)


def test_transition_to_raises_on_disallowed_backwards_move():
    booking = _make_booking(BookingStatus.READY)
    with pytest.raises(IllegalStatusTransitionError):
        booking.transition_to(BookingStatus.PENDING)


def test_transition_to_noop_same_status():
    booking = _make_booking(BookingStatus.PENDING)
    booking.transition_to(BookingStatus.PENDING)  # must not raise
    assert booking.status == BookingStatus.PENDING


def test_transition_to_valid_pending_to_provisioning():
    booking = _make_booking(BookingStatus.PENDING)
    booking.transition_to(BookingStatus.PROVISIONING)
    assert booking.status == BookingStatus.PROVISIONING


def test_transition_to_valid_queued_to_ready():
    booking = _make_booking(BookingStatus.QUEUED)
    booking.transition_to(BookingStatus.READY)
    assert booking.status == BookingStatus.READY


def test_transition_to_valid_releasing_to_released():
    booking = _make_booking(BookingStatus.RELEASING)
    booking.transition_to(BookingStatus.RELEASED)
    assert booking.status == BookingStatus.RELEASED


# ── _to_entity unknown status (I7) ────────────────────────────────────────────

def test_to_entity_raises_on_unrecognised_status():
    from app.infrastructure.repositories.booking_repo import _to_entity
    from app.infrastructure.database.models import BookingModel

    model = BookingModel()
    model.id = uuid4()
    model.user_id = str(uuid4())
    model.status = "INVALID_STATUS"
    model.resource_type = ResourceType.VM.value
    model.ttl_minutes = 60
    model.expires_at = datetime.now(timezone.utc)
    model.created_at = datetime.now(timezone.utc)
    model.config_roles = []
    model.extra_vars = {}
    model.config_failed = False

    with pytest.raises(ValueError, match="unrecognised status"):
        _to_entity(model)


# ── _assign_resource_and_ready guard (D2) ─────────────────────────────────────

def test_assign_resource_raises_when_not_queued():
    from app.infrastructure.repositories.booking_repo import _assign_resource_and_ready
    from app.infrastructure.database.models import BookingModel, NamespaceModel

    booking_model = MagicMock(spec=BookingModel)
    booking_model.id = uuid4()
    booking_model.status = BookingStatus.PENDING.value  # PENDING → READY is not allowed
    booking_model.ttl_minutes = 60

    resource = MagicMock(spec=NamespaceModel)
    resource.id = uuid4()

    session = MagicMock()

    with pytest.raises(IllegalStatusTransitionError):
        _assign_resource_and_ready(
            session, booking_model, ResourceType.NAMESPACE.value, resource
        )


def test_assign_resource_succeeds_when_queued():
    from app.infrastructure.repositories.booking_repo import _assign_resource_and_ready
    from app.infrastructure.database.models import BookingModel, NamespaceModel

    booking_model = MagicMock(spec=BookingModel)
    booking_model.id = uuid4()
    booking_model.status = BookingStatus.QUEUED.value
    booking_model.ttl_minutes = 60

    resource = MagicMock(spec=NamespaceModel)
    resource.id = uuid4()

    session = MagicMock()

    _assign_resource_and_ready(
        session, booking_model, ResourceType.NAMESPACE.value, resource
    )
    assert booking_model.status == BookingStatus.READY.value
