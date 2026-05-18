# Feature: Per-User Timezone Setting

## Goal

Allow each user to select a preferred IANA timezone. All UTC timestamps displayed in the UI
(booking expiry time) are converted to that timezone. Storage and business logic remain UTC.

## What Changes

### DB

New column on `users`:

```
timezone  VARCHAR(64) NOT NULL DEFAULT 'UTC'
```

Alembic migration: `0006_user_timezone.py`

### Domain

`User` entity gains `timezone: str = "UTC"`.

### Infrastructure

- `UserModel` gains `timezone: Mapped[str]` column.
- `UserRepository` — new method `async update_timezone(session, user_id, timezone) -> None`.
- Jinja2 templates environment gets a custom filter `as_tz(dt, tz)` registered in `app/main.py`
  that converts a UTC-aware datetime to the given IANA timezone and formats it as
  `YYYY-MM-DD HH:MM (TZ)`.

### Routes

Two new endpoints added to `app/presentation/routes/auth.py`:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/profile` | `require_user` | Render profile page with timezone selector |
| POST | `/profile` | `require_user` | Save selected timezone, redirect back to `/profile` |

### Templates

- `app/presentation/templates/profile.html` — new page. Dark-themed form with a `<select>` of
  sorted IANA timezone names (from `zoneinfo.available_timezones()`), current value pre-selected.
  Success flash message shown after save.
- `app/presentation/templates/base.html` — add "Profile" link in the header nav next to the
  Sign out button.
- `app/presentation/templates/partials/booking_row.html` — replace:
  ```
  {{ booking.expires_at.strftime('%Y-%m-%d %H:%M') }} UTC
  ```
  with:
  ```
  {{ booking.expires_at | as_tz(current_user.timezone) }}
  ```
  The `booking_row` partial is rendered both on full page load (has `current_user`) and via
  HTMX polling (`GET /bookings/{id}/row`). The row endpoint already receives `current_user`
  via `Depends(require_user)` — it just needs to pass it to the template context.

### Validation

Submitted timezone value must be in `zoneinfo.available_timezones()`. Invalid values return
a 400 with a form-level error message.

## Expected Behaviour / Edge Cases

- Default timezone is `UTC`; existing users see no change until they update their preference.
- The `as_tz` filter handles naive datetimes by assuming UTC before converting.
- Timezone list is sorted alphabetically in the dropdown.
- The `/bookings/{id}/row` polling endpoint must pass `current_user` to the template so the
  correct timezone is used on live updates — this is already available via the auth dependency.
- No change to JSON API responses; those always return ISO-8601 UTC.
