# Feature: Booking Filter (#102)

## Goal

The VM list currently shows all users' bookings. Default it to showing only the
current user's bookings, with a toggle to see all. No DB change required.

## Behaviour

- Default: `GET /` shows only the current user's bookings
- Toggle button **"All VMs"** switches to all bookings (`?filter=all`)
- Toggle button **"My VMs"** switches back to own bookings (`?filter=mine`)
- Active filter is reflected in the URL so the page is bookmarkable
- Admins follow the same default (their own bookings), but can switch to All

## Repository change

`app/infrastructure/repositories/booking_repo.py` — add:

```python
async def list_by_user(self, session: AsyncSession, user_id: str) -> list[Booking]:
```

Filters `BookingModel.user_id == user_id`. Existing `list_all()` unchanged.

## Route change

`app/presentation/routes/bookings.py` — `GET /` accepts `?filter=mine|all`
(default: `mine`):

```python
@router.get("/")
async def index(request, filter: str = "mine", ...):
    if filter == "all":
        bookings = await _repo.list_all(session)
    else:
        bookings = await _repo.list_by_user(session, str(current_user.id))
```

## UI change

`app/presentation/templates/index.html` — filter toggle above the bookings table:

```
[ My VMs ]  [ All VMs ]
```

- Active tab has green underline; inactive is muted
- Each button is `hx-get="/?filter=mine"` / `hx-get="/?filter=all"` targeting
  only the bookings section so the booking form stays untouched

## Files changed

| File | Change |
|------|--------|
| `app/infrastructure/repositories/booking_repo.py` | Add `list_by_user(session, user_id)` |
| `app/presentation/routes/bookings.py` | Accept `filter` query param in index route |
| `app/presentation/templates/index.html` | Filter toggle above bookings table |

## Tests

- `GET /` returns only current user's bookings (default `mine`)
- `GET /?filter=all` returns all bookings
- `GET /?filter=mine` returns only current user's bookings
