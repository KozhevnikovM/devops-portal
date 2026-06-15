"""Unit tests for the Lease value object (#238)."""
from datetime import datetime, timedelta, timezone

from app.domain.constants import PERMANENT_EXPIRES_AT
from app.domain.lease import Lease


def test_starting_now_timed():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lease = Lease.starting_now(60, now=now)
    assert lease.ttl_minutes == 60
    assert lease.expires_at == now + timedelta(minutes=60)
    assert lease.is_permanent is False


def test_starting_now_permanent():
    lease = Lease.starting_now(0)
    assert lease.is_permanent is True
    assert lease.expires_at == PERMANENT_EXPIRES_AT


def test_starting_now_defaults_to_utc_now():
    before = datetime.now(timezone.utc)
    lease = Lease.starting_now(30)
    after = datetime.now(timezone.utc)
    assert before + timedelta(minutes=30) <= lease.expires_at <= after + timedelta(minutes=30)


def test_pending_is_far_future_placeholder():
    lease = Lease.pending(240)
    assert lease.ttl_minutes == 240
    assert lease.expires_at == PERMANENT_EXPIRES_AT


def test_extended_by_minutes():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lease = Lease.starting_now(60, now=now).extended_by(30)
    assert lease.ttl_minutes == 90
    assert lease.expires_at == now + timedelta(minutes=90)


def test_extended_by_zero_makes_permanent():
    lease = Lease.starting_now(60).extended_by(0)
    assert lease.is_permanent is True
    assert lease.expires_at == PERMANENT_EXPIRES_AT


def test_boundary_ttl_zero_is_permanent():
    assert Lease.starting_now(0).is_permanent
    assert not Lease.starting_now(1).is_permanent
