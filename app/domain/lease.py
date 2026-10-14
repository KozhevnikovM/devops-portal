from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.booking_status import CAN_BECOME_READY
from app.domain.constants import MAX_TTL_MINUTES, PERMANENT_EXPIRES_AT
from app.domain.enums import BookingStatus
from app.domain.exceptions import InvalidExtensionError


@dataclass(frozen=True)
class Lease:
    """The window a booking holds its resource. ``ttl_minutes == 0`` means permanent.

    The single home for the lease/TTL rule that used to be copy-pasted across the use cases and
    repositories: *permanent → far-future sentinel, otherwise ``now + ttl_minutes``*.
    """

    ttl_minutes: int
    expires_at: datetime

    @property
    def is_permanent(self) -> bool:
        return self.ttl_minutes == 0

    @classmethod
    def starting_now(cls, ttl_minutes: int, *, now: datetime | None = None) -> "Lease":
        """A lease whose clock starts now (or at ``now`` if given)."""
        now = now or datetime.now(UTC)
        expires = (
            PERMANENT_EXPIRES_AT
            if ttl_minutes == 0
            else now + timedelta(minutes=ttl_minutes)
        )
        return cls(ttl_minutes=ttl_minutes, expires_at=expires)

    @classmethod
    def pending(cls, ttl_minutes: int) -> "Lease":
        """Not yet started (QUEUED / pre-READY environment): a far-future placeholder expiry. The
        clock starts later — on queue promotion, or when the stack is READY — via ``starting_now``.
        ``enforce_ttl`` ignores such bookings by status, so the placeholder is never enforced."""
        return cls(ttl_minutes=ttl_minutes, expires_at=PERMANENT_EXPIRES_AT)

    def extended_by(self, minutes: int) -> "Lease":
        """A new lease pushed out by ``minutes`` (0 → made permanent)."""
        if minutes == 0:
            return Lease(0, PERMANENT_EXPIRES_AT)
        return Lease(
            self.ttl_minutes + minutes, self.expires_at + timedelta(minutes=minutes)
        )

    def extended_within_term_by(self, minutes: int) -> "Lease":
        """A timed lease pushed out by a positive ``minutes`` — never made permanent (#532).

        Unlike ``extended_by`` (whose ``0 → permanent`` is the standalone booking "Forever"
        option), this rejects a nonpositive increment, and any result whose expiry would reach the
        permanent sentinel or whose TTL would not fit the database, with ``InvalidExtensionError``.
        """
        if minutes <= 0:
            raise InvalidExtensionError(
                "extend_minutes must be a positive number of minutes"
            )
        try:
            expires_at = self.expires_at + timedelta(minutes=minutes)
        except OverflowError:  # past datetime.max
            expires_at = PERMANENT_EXPIRES_AT
        if (
            expires_at >= PERMANENT_EXPIRES_AT
            or self.ttl_minutes + minutes > MAX_TTL_MINUTES
        ):
            raise InvalidExtensionError("extend_minutes is too large")
        return Lease(self.ttl_minutes + minutes, expires_at)


def lease_can_start(statuses: Iterable[BookingStatus]) -> bool:
    """Whether an environment's shared lease may start, given its children's statuses (#434).

    True once every child has *settled* — none can still become READY (see ``CAN_BECOME_READY``)
    — and at least one child is READY (something live for the lease to bound). A FAILED, RELEASED
    or RELEASING child therefore never holds the lease back forever.
    """
    statuses = list(statuses)
    return not any(s in CAN_BECOME_READY for s in statuses) and any(
        s == BookingStatus.READY for s in statuses
    )


def environment_extend_blocker(status: BookingStatus, lease: Lease) -> str | None:
    """Why an environment can't have its shared lease extended, or ``None`` if it can (#532).

    ``status`` is the environment's derived status. Only a READY, timed stack whose lease has
    already started may be extended — a still-pending lease has no deadline to push out yet, and a
    FAILED / partly released / RELEASED stack is not extended (matching the READY-only booking rule).
    """
    if lease.is_permanent:
        return "cannot extend a permanent environment"
    if status != BookingStatus.READY:
        return "can only extend READY environments"
    if lease.expires_at == PERMANENT_EXPIRES_AT:
        return "the environment's lease has not started yet"
    return None
