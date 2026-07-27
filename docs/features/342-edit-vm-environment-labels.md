# Feature: Edit label on VM bookings; edit name on Environments (#342)

## Goal

Let the owner (or an admin, or the dispatcher who created it on the owner's behalf) rename
a VM booking's label or an Environment's name after creation, instead of only at creation
time. Closes #342.

## Current behaviour

- `Booking.label` exists and can be set at creation (`POST /bookings`, `POST /api/bookings`)
  but there is no endpoint to change it afterward.
- `Environment.name` is set at order time (`POST /environments`, `POST /api/environments`)
  with no rename capability.
- The only post-creation mutations today are `PUT /api/bookings/{id}/extend` (TTL) and
  release/delete — no `PATCH`-style endpoint exists for either resource.

## What changes

No DB migration — both `bookings.label` and `environments.name` columns already exist.

### 1. `app/application/use_cases/update_booking_label.py` (new)

`UpdateBookingLabelUseCase(repo: BookingRepositoryPort)`:
- `execute(session, booking_id, label: str | None, current_user: User) -> Booking`
- Reuses `can_manage()` (`app/application/use_cases/_permissions.py`) for authorization —
  same rule as extend/release: owner, creating dispatcher, or admin. Raises
  `BookingPermissionError` otherwise.
- Strips whitespace; rejects a label over 128 chars with `BookingError` (matches the
  existing `bookings.label` column width, same limit enforced at booking-creation time).
- Empty string is normalized to `None` (clears the label) — mirrors how `label` is
  already treated as optional at creation.
- No status restriction — a label can be edited regardless of booking status (including
  RELEASED), since it's just a display annotation, not operational state.

### 2. `app/application/use_cases/update_environment_name.py` (new)

`UpdateEnvironmentNameUseCase(repo: EnvironmentRepositoryPort)`:
- `execute(session, environment_id, name: str, current_user: User) -> Environment`
- Same `can_manage()` check, reusing the existing `BookingPermissionError` (already used
  by `ReleaseEnvironmentUseCase` for environments — no new exception type).
- `name` is required (column is `NOT NULL`) — rejects blank/whitespace-only with
  `EnvironmentError`; enforces the same 128-char limit as the column.

### 3. `app/infrastructure/repositories/booking_repo.py`

Add `async def update_label(session, booking_id, label: str | None, actor_id: str) -> None`:
- Loads the model, sets `model.label = label`, writes a `BookingAuditModel` row with
  `action="LABEL_CHANGED"`, `old_status=new_status=None`, `extra={"old_label": ..., "new_label": ...}`
  (mirrors the existing `update_status()` audit pattern), commits.

### 4. `app/infrastructure/repositories/environment_repo.py`

Add `async def update_name(session, environment_id, name: str) -> None`:
- Loads the model, sets `model.name = name`, commits. No audit entry — environments have
  no audit table today (out of scope; child-booking audits are unaffected).

### 5. `app/application/ports.py`

Add `update_label` to `BookingRepositoryPort` and `update_name` to `EnvironmentRepositoryPort`.

### 6. `app/presentation/deps.py`

Wire `update_booking_label_uc = UpdateBookingLabelUseCase(booking_repo)` and
`update_environment_name_uc = UpdateEnvironmentNameUseCase(environment_repo)`.

### 7. API routes — `app/presentation/routes/api_bookings.py` / `api_environments.py`

- `PATCH /api/bookings/{booking_id}` — body `{"label": str | null}` → 200 with the same
  shape as `GET /api/bookings/{id}`.
- `PATCH /api/environments/{environment_id}` — body `{"name": str}` → 200 with the same
  shape as `GET /api/environments/{id}`.
- Both mounted under `/api/v1/...` too (existing dual-mount pattern from F-6).
- 404 if not found, 403 via `BookingPermissionError` → `HTTPException(403)`, 400 on
  validation errors (`BookingError`/`EnvironmentError`).

### 8. HTMX routes — `app/presentation/routes/routes.py` / `environments.py`

- `PATCH /bookings/{booking_id}/label` (form field `label`) → returns
  `partials/booking_row.html` (same pattern as extend/release fragments).
- `PATCH /environments/{environment_id}/name` (form field `name`) → returns
  `partials/environment_row.html`.
- Errors re-render the row with an inline error via the existing `hx_error()` helper
  (`app/presentation/utils.py`, added in v0.11.0 F-8).

### 9. Templates

- `partials/booking_row.html`: label display (`booking.label`) becomes a small
  pencil-icon-triggered inline edit form (HTMX `hx-patch`), following the same
  inline-edit interaction already used elsewhere in the row (e.g. extend-TTL control).
- `partials/environment_row.html`: same treatment for `environment.name`.

## Expected behaviour / edge cases

| Case | Result |
|---|---|
| Owner edits their own booking's label | 200/updated row, new label shown |
| Admin edits another user's booking/environment label | Allowed (same rule as extend/release) |
| Non-owner, non-admin, non-creating-dispatcher attempts edit | 403 |
| Label cleared (empty string submitted) | Booking label becomes `null` |
| Label > 128 chars | 400, no change persisted |
| Environment name submitted blank | 400, no change persisted (name is required) |
| Booking/environment not found | 404 |
| Edited while booking is RELEASED/FAILED | Allowed — label is a display annotation, not operational state |

## Out of scope

- Audit trail for environment name changes (no environment audit table exists; adding
  one is a separate concern).
- Editing any field other than `label` (booking) / `name` (environment) — e.g. no
  re-labeling of individual environment child-booking `environment_label`s in this pass.

## Docs to update after implementation

- `docs/api-reference.md` — new `PATCH` endpoints.
- `docs/admin-guide.md` — brief mention if admin-specific behaviour differs (it doesn't;
  admins just get the same edit action on any resource).
