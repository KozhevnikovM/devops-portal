# Feature: Human-readable label on VM booking creation (#107)

## Goal

Let users give a booking a short, memorable label (e.g. "k8s node 3", "my perf test") when
creating it. The label is displayed alongside the booking in the UI and returned in API
responses, making it easier to identify specific bookings in a list.

## What changes

### Domain

**`app/domain/entities.py`** — `Booking` dataclass
- Add `label: str | None = None`.

### DB

**New Alembic migration** — add `label VARCHAR(128) NULL` to the `bookings` table.
No existing rows are affected (nullable, default NULL).

### Infrastructure

**`app/infrastructure/database/models.py`** — `BookingModel`
- Add `label: Mapped[str | None] = mapped_column(String(128), nullable=True)`.

**`app/infrastructure/repositories/booking_repo.py`**
- `_to_entity`: map `m.label → booking.label`.
- `create` (async + sync variants): persist `label` from the entity.

### Application

**`app/application/use_cases/create_booking.py`**
- Accept `label: str | None` in the use case call signature (alongside `image_id`, `ttl_minutes`,
  etc.).
- Pass it through to the `Booking` entity constructor.

### Presentation

**`app/presentation/routes/bookings.py`** (HTMX route) and
**`app/presentation/routes/api.py`** (JSON route) — `POST /bookings`
- Read optional `label` from the form body / JSON payload.
- Validate: max 128 chars; strip whitespace. Empty string treated as `None`.
- Forward to `CreateBookingUseCase`.

**`app/presentation/templates/partials/booking_form.html`**
- Add an optional text input: `Label (optional)` — a single line, max 128 chars.
- Placed near the top of the form, before resource-type selection.

**`app/presentation/templates/partials/booking_row.html`**
- If `booking.label` is set, render it as a small text line below the booking ID / resource name.

**`app/presentation/schemas.py`** (if a Pydantic schema is used for `POST /api/bookings`)
- Add `label: str | None = None` with `max_length=128`.

## Expected behaviour / edge cases

- **Label provided** → stored, shown in the row, returned in JSON responses.
- **Label omitted** → `NULL` in DB; row shows just resource name as today.
- **Label > 128 chars** → 422 Unprocessable Entity.
- **Environments**: child bookings created via `OrderEnvironmentUseCase` receive no label by
  default (environment already has a `name`). The field stays `NULL` for them.
- **Existing bookings** (no label) display unchanged.

## API change

`POST /api/bookings` — accepts optional `label` field in the JSON body.
`GET /api/bookings`, `GET /api/bookings/{id}` — `label` field added to the response (nullable).

Update `docs/api-reference.md` to document the new field.

## Test

- `POST /api/bookings` with `label="my test vm"` → `GET /api/bookings/{id}` returns same label.
- `POST /api/bookings` with label > 128 chars → 422.
- `POST /api/bookings` without label → label is null in response.
