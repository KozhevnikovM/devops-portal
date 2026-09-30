from datetime import datetime, timezone

# Expiry timestamp used for "permanent" bookings (ttl_minutes == 0). A fixed far-future
# sentinel so a single SQL ordering/comparison handles permanent and temporary bookings alike.
PERMANENT_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

# Cap on a booking's provisioning log (#378). Only the most recent characters are kept. Progress
# batching (#444) reuses it as the hard bound on buffered log text and on the status message a
# progress line sets.
PROVISIONING_LOG_MAX_CHARS = 50_000
