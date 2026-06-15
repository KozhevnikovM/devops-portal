from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.constants import PERMANENT_EXPIRES_AT


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
        now = now or datetime.now(timezone.utc)
        expires = PERMANENT_EXPIRES_AT if ttl_minutes == 0 else now + timedelta(minutes=ttl_minutes)
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
        return Lease(self.ttl_minutes + minutes, self.expires_at + timedelta(minutes=minutes))
