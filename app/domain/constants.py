from datetime import UTC, datetime

# Expiry timestamp used for "permanent" bookings (ttl_minutes == 0). A fixed far-future
# sentinel so a single SQL ordering/comparison handles permanent and temporary bookings alike.
PERMANENT_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

# Cap on a booking's provisioning log (#378). Only the most recent characters are kept. Progress
# batching (#444) reuses it as the hard bound on buffered log text and on the status message a
# progress line sets.
PROVISIONING_LOG_MAX_CHARS = 50_000

# Largest lease TTL the database can hold: ttl_minutes is a 32-bit INTEGER column. An extension
# that would exceed it is rejected as invalid input rather than failing on write (#532).
MAX_TTL_MINUTES = 2**31 - 1
