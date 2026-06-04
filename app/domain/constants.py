from datetime import datetime, timezone

# Expiry timestamp used for "permanent" bookings (ttl_minutes == 0). A fixed far-future
# sentinel so a single SQL ordering/comparison handles permanent and temporary bookings alike.
PERMANENT_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
