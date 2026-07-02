# Bugfix: OverflowError on environments page when expires_at is the pending sentinel

## Root cause

`Lease.pending()` (and `Lease.starting_now()` when `ttl_minutes == 0`) sets `expires_at` to
`PERMANENT_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)`.  This sentinel is also
used as a placeholder for environments that haven't reached READY yet.

The `environment_row.html` template only guards against this for `derived_status == 'PROVISIONING'`
(shows "starts when ready").  For any other pre-READY status the template falls through to:

```jinja
{{ environment.expires_at | as_tz(current_user.timezone) }}
```

`_as_tz` calls `dt.astimezone(tz)`.  For UTC+ timezones (e.g. Europe/Moscow = +3 h) adding the
offset to `9999-12-31 23:59:59 UTC` pushes the result beyond the maximum representable Python
`datetime`, raising `OverflowError: date value out of range` and causing a 500 on
`GET /dp/environments`.

## What changes

**`app/presentation/templating.py`** — catch `OverflowError` in `_as_tz` and return `"—"` when
the sentinel cannot be converted.  This is the correct layer to fix: the filter is the one doing
the conversion and is the right place to handle the boundary of representable datetimes.

The template already renders `Never` for `ttl_minutes == 0` before calling the filter, so for
truly-permanent bookings the filter is never reached.  For the pending-placeholder case (non-zero
TTL, pre-READY) the fallback `"—"` signals "expiry not yet determined" to the user.

## Expected behaviour after fix

- Environments page loads without error for users in UTC+ (or any non-UTC) timezone.
- A pre-READY environment with a non-zero TTL shows `—` in the expires column instead of crashing.
- No change to the permanent (`ttl_minutes == 0`) or already-READY display paths.

## Test coverage

Add a unit test for `_as_tz` that passes `PERMANENT_EXPIRES_AT` with a UTC+ timezone and asserts
it returns `"—"` without raising.
