# Feature: Typed NotFoundError domain exception (P3-E, Issue #304)

## Goal

Repositories signal "record not found" by raising bare `ValueError`. Routes catch
`except ValueError → 404`, which is fragile: a real programming error (e.g. a bad
format string, an unexpected None dereference) that happens to raise `ValueError` inside
a repo call silently becomes a 404 instead of a 500. Two routes go further and sniff the
string `"not found"` to distinguish not-found from other ValueErrors — which breaks the
moment a message is rephrased.

Introduce `NotFoundError` in `app/domain/exceptions.py`. Repos raise it instead of
`ValueError`. Routes catch it explicitly. The blanket `except ValueError → 404` is removed.

## What changes

### `app/domain/exceptions.py`

Add a base `NotFoundError` and per-entity subclasses. `BookingNotFoundError` already
exists; add the rest:

```python
class NotFoundError(DomainError): ...

class BookingNotFoundError(NotFoundError): ...     # already exists — reparent
class EnvironmentNotFoundError(NotFoundError): ...  # move from release_environment.py
class NamespaceNotFoundError(NotFoundError): ...
class StaticVMNotFoundError(NotFoundError): ...
class ImageNotFoundError(NotFoundError): ...
class HWConfigNotFoundError(NotFoundError): ...
class RoleNotFoundError(NotFoundError): ...
class BlueprintNotFoundError(NotFoundError): ...    # already exists — reparent
```

### Repositories (8 files)

Replace every `raise ValueError("... not found")` with the appropriate typed exception:

| Repo | Old | New |
|------|-----|-----|
| `booking_repo.py` | `BookingNotFoundError` (already typed) | unchanged |
| `environment_repo.py` | `ValueError` | `EnvironmentNotFoundError` |
| `namespace_repo.py` | `ValueError` | `NamespaceNotFoundError` |
| `static_vm_repo.py` | `ValueError` | `StaticVMNotFoundError` |
| `image_repo.py` | `ValueError` | `ImageNotFoundError` |
| `hw_config_repo.py` | `ValueError` | `HWConfigNotFoundError` |
| `role_repo.py` | `ValueError` | `RoleNotFoundError` |
| `environment_blueprint_repo.py` | `ValueError` | `BlueprintNotFoundError` |

### `app/application/use_cases/release_environment.py`

Remove the local `EnvironmentNotFoundError` definition and import from `exceptions.py`.

### Routes (6 files)

Replace `except ValueError → 404` with `except NotFoundError → 404`. Remove the two
string-sniff patterns (`"not found" in str(exc).lower()`):

- `app/presentation/routes/admin.py` — ~30 handlers
- `app/presentation/routes/api.py` — ~8 handlers
- `app/presentation/routes/environments.py` — 3 handlers
- `app/presentation/routes/api_environments.py` — 2 handlers
- `app/presentation/routes/bookings.py` — already catches `BookingNotFoundError` ✓
- `app/presentation/routes/api_bookings.py` — already catches `BookingNotFoundError` ✓

`auth.py` raises 404 directly (no repo ValueError path) — no change needed.

## Expected behaviour after the change

- A real `ValueError` (format error, type mismatch) propagating out of a repo call
  becomes a 500, not a silent 404.
- Not-found responses remain 404 with the same messages.
- No user-visible change for the happy path or genuine not-found cases.

## Regression tests

- Repo raises `NotFoundError` (not `ValueError`) when a record is missing.
- Route converts `NotFoundError` → 404.
- A plain `ValueError` from within a route handler is not caught as a 404
  (propagates as 500 / is re-raised).
- `BookingNotFoundError` and `BlueprintNotFoundError` remain catchable as `NotFoundError`.
