# Feature: Hide RELEASED Bookings by Default (#110)

## Goal

The bookings table fills up with RELEASED rows over time, making active VMs harder
to find. Hide RELEASED bookings by default and provide a toggle to show them.

This filter is **independent** of the owner filter (#102, `?filter=mine|all`) — the
two compose: a user can view "My VMs" with or without released, or "All VMs" with or
without released.

## Behaviour

- Default view: RELEASED bookings are hidden.
- A toggle button above the table reveals them: **"Show released"** → **"Hide released"**.
- State is preserved in the URL as `?show_released=1` so the page is bookmarkable and
  survives refresh.
- Combines with the owner filter, e.g. `/?filter=all&show_released=1`.

## No DB change

Pure read-side filter. No migration.

## Repository change (`app/infrastructure/repositories/booking_repo.py`)

Add an `include_released: bool = False` parameter to both list methods so the default
(no arg) hides RELEASED rows:

```python
async def list_all(self, session, include_released: bool = False) -> list[Booking]: ...
async def list_by_user(self, session, user_id, include_released: bool = False) -> list[Booking]: ...
```

When `include_released` is `False`, add `.where(BookingModel.status != BookingStatus.RELEASED.value)`.

## Route change (`app/presentation/routes/bookings.py`)

`GET /` index accepts `show_released: bool = False` alongside the existing `filter`:

```python
@router.get("/")
async def index(filter: str = "mine", show_released: bool = False, ...):
    if filter == "all":
        bookings = await _repo.list_all(session, include_released=show_released)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id), include_released=show_released)
    ...
    {"active_filter": filter, "show_released": show_released, ...}
```

FastAPI coerces `?show_released=1` / `true` → `True`; omitted → `False`.

## UI change (`app/presentation/templates/index.html`)

Add a third toggle button in the existing filter button group, next to "My VMs" /
"All VMs". It must preserve the current owner filter in its `hx-get` URL:

```
[ My VMs ] [ All VMs ]    [ Show released / Hide released ]
```

- When `show_released` is off: button reads **"Show released"**, links to
  `/?filter={{ active_filter }}&show_released=1`.
- When on: button reads **"Hide released"** (active/highlighted styling), links to
  `/?filter={{ active_filter }}` (drops the param).
- Same `hx-target="#bookings-section"`, `hx-select="#bookings-section"`,
  `hx-push-url="true"` as the existing buttons.
- The "My VMs" / "All VMs" buttons must likewise carry `&show_released=1` when it is
  currently on, so switching owner doesn't silently drop the released filter.

## Files changed

| File | Change |
|------|--------|
| `app/infrastructure/repositories/booking_repo.py` | `include_released` param on `list_all` / `list_by_user` |
| `app/presentation/routes/bookings.py` | `show_released` query param; pass through + to template |
| `app/presentation/templates/index.html` | "Show/Hide released" toggle; preserve filter+show_released across buttons |

## Tests

- `GET /` (default) excludes RELEASED bookings
- `GET /?show_released=1` includes RELEASED bookings
- `GET /?filter=all` (default released off) excludes RELEASED across all users
- `GET /?filter=all&show_released=1` includes RELEASED across all users
- Repository: `list_all(include_released=False)` filters out RELEASED;
  `include_released=True` returns them

## Docs

- `docs/api-reference.md` — document the `show_released` query param on `GET /`
