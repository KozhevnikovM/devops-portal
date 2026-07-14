# Bugfix: JSON `create_user` and admin UI bypass ≥8-char password check (Issue #295)

## Root cause

Two of the five password-setting paths in `auth.py` have no minimum length check:

| Path | Check? |
|---|---|
| `POST /api/users` (`create_user`, line 159) | **missing** |
| `POST /admin/users` (`admin_create_user`, line 287) | **missing** |
| `POST /api/users/{id}/password` (`admin_reset_password`, line 178) | ✓ 422 if < 8 |
| `POST /admin/users/{id}/password` (`admin_reset_user_password_ui`, line 365) | ✓ HTML error |
| `POST /profile/password` (`change_password`, line 480) | ✓ HTML error |

An admin calling `POST /api/users` (e.g. from a Jenkins pipeline or curl script) can create
a user with a 1-character password; the check is never applied before the bcrypt hash is
written to the database.

The admin UI path (`admin_create_user`) has the same omission, so a logged-in admin can
also bypass the check via the web form.

## Fix

Add a length check to each missing path:

- `create_user` (JSON): `if len(body.password) < 8: raise HTTPException(status_code=422, detail="password must be at least 8 characters")`
- `admin_create_user` (HTML form): return an HTML error fragment (same pattern as the
  `IntegrityError` case already in that handler)

No shared helper is needed — the two paths have different return types (JSON vs HTML)
and the check is a one-liner each.

**Files**: `app/presentation/routes/auth.py`

## Expected behaviour after fix

- `POST /api/users` with `password` shorter than 8 chars → `422 Unprocessable Entity`.
- `POST /admin/users` with a short password → inline HTML error, no user created.
- All other paths (already checked) are unaffected.

## Test (regression)

`tests/test_create_user_password_check.py`:

1. `POST /api/users` with 3-char password → 422.
2. `POST /api/users` with exactly 7-char password → 422.
3. `POST /api/users` with exactly 8-char password → 201 (boundary passes).
4. `POST /api/users` with 12-char password → 201.
