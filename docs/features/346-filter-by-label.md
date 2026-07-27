# Feature: Filter bookings and environments by label (#346)

## Goal

Let users narrow the bookings list and the environments list by label, on top of the
existing `mine`/`all` and `show_released` filters. Closes #346.

## Current behaviour

- `GET /book/vm`, `GET /book/namespace` (HTMX) and `GET /api/bookings` (JSON) support
  `filter=mine|all` and `show_released`, applied in `BookingRepository.list_all()` /
  `list_by_user()` (`app/infrastructure/repositories/booking_repo.py`) via
  `_apply_resource_type_filter()`. No label filter exists anywhere in that path.
- `GET /environments` (HTMX) and `GET /api/environments` (JSON) have the same two filters,
  applied in `EnvironmentRepository.list_all()` / `list_by_user()` /
  `_list()`. No label filter exists there either.
- As settled in #345, an Environment's `name` already serves as its label (renamed via
  `PATCH /api/environments/{id}`, #342/#344) — there is no separate `label` column on
  environments, so filtering "by label" for environments means filtering by `name`.

## What changes

No DB migration — filters against existing `bookings.label` and `environments.name` columns,
case-insensitive substring match (`ILIKE '%...%'`). No new index: this is a small-scale
admin/user tool, not a hot path, and a leading-wildcard `ILIKE` can't use a btree index
anyway, so adding one now would be premature.

### 1. `app/infrastructure/repositories/booking_repo.py`

Add a `_apply_label_filter(stmt, label: str | None)` helper (mirrors the existing
`_apply_resource_type_filter`): no-op if `label` is `None` or blank after `.strip()`,
otherwise `stmt.where(BookingModel.label.ilike(f"%{label.strip()}%"))`.

Add `label: str | None = None` to `list_all()` and `list_by_user()`; apply the new filter
alongside the existing resource-type filter.

### 2. `app/infrastructure/repositories/environment_repo.py`

Same shape: add `label: str | None = None` to `list_all()`, `list_by_user()`, and the
shared `_list()` they both call; filter on `EnvironmentModel.name.ilike(f"%...%")`.

### 3. Routes

- `app/presentation/routes/bookings.py`: add `label: str | None = None` (query param) to
  `vm_bookings_page()` and `namespace_bookings_page()`; thread through
  `_render_bookings_page()` into the two repo calls. Pass `label_filter=label` into the
  template context (named `label_filter` to avoid confusion with a booking's own `label`
  field, which the row template already renders as `booking.label`).
- `app/presentation/routes/api_bookings.py`: add `label: str | None = None` query param to
  `list_bookings()`.
- `app/presentation/routes/environments.py`: add `label: str | None = None` to
  `environments_page()`; thread through `_list_for()`. Same `label_filter` context naming.
- `app/presentation/routes/api_environments.py`: add `label: str | None = None` query
  param to `list_environments()`.

### 4. Templates

- `index.html` and `environments.html`: add a text `<input name="label">` next to the
  existing Mine/All/Show-released buttons, with `hx-get`, `hx-trigger="keyup changed delay:400ms, search"`,
  `hx-target`/`hx-select` matching the existing buttons, and `hx-push-url="true"` so the
  filter survives refresh/bookmarking (same as `filter`/`show_released` today). The input's
  own `name="label"` attribute means htmx includes its current value automatically when it
  fires the request — no `hx-include` needed.
- The three existing Mine/All/Show-released buttons gain `&label={{ label_filter | urlencode }}`
  (only when set) to their hardcoded query strings, so switching those toggles doesn't drop
  an active label filter.

## Expected behaviour / edge cases

| Case | Result |
|---|---|
| `label=perf` on `/api/bookings` | Bookings whose `label` contains "perf" (case-insensitive) |
| `label=dev` on `/api/environments` | Environments whose `name` contains "dev" |
| `label` omitted or blank | No filtering — identical to today |
| Label filter combined with `filter=all`/`show_released` | Filters compose (AND) |
| No bookings/environments match | Empty list, same as any other filter combination that matches nothing |

## Out of scope

- Exact match, multi-label tagging, or a dedicated label catalog — this keeps the existing
  single free-text label/name model; only the matching mode (substring) is new.
- A label filter on the environment blueprint's per-child `environment_label` — those are
  admin-defined display names within a stack, not the top-level resource identity this
  issue is about.

## Docs to update after implementation

- `docs/api-reference.md` — add `label` to the query parameter tables for
  `GET /api/bookings` and `GET /api/environments`.
