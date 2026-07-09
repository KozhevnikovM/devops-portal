# Feature: Change / reset password (#290)

## Goal

- **Users** can change their own password from the profile page.
- **Admins** can reset any user's password without knowing the current one.

---

## What changes

### 1. Session invalidation on password change

Currently Redis sessions are stored as `session:{id}` → JSON with no reverse index.
To invalidate all of a user's sessions on password change, maintain a Redis set
`user_sessions:{user_id}` that tracks active session IDs:

- **Login** (`POST /auth/login`): after writing `session:{id}`, also `SADD user_sessions:{uid} {id}`.
- **Logout** (`POST /auth/logout`): also `SREM user_sessions:{uid} {id}`.
- **Password change**: `SMEMBERS user_sessions:{uid}` → delete each `session:*` key → `DEL user_sessions:{uid}`.
  For self-service, the session that made the change request is re-issued so the user stays logged in.

No DB migration needed — this is Redis-only.

### 2. UserRepository — new `update_password` method

```python
async def update_password(self, session: AsyncSession, user_id: UUID, new_hash: str) -> None:
```

Updates `users.password_hash` and commits. No domain event needed.

### 3. Self-service: `POST /profile/password`

HTMX route on the existing profile page. Body (form-encoded):
- `current_password` (required)
- `new_password` (required, min 8 chars)

Flow:
1. Fetch user from DB; verify `current_password` against `password_hash` with `bcrypt.checkpw`.
2. On mismatch → return the password section with an error banner (no redirect).
3. Hash `new_password` with `bcrypt.hashpw`; call `repo.update_password`.
4. Invalidate all other sessions for this user (keep current session alive).
5. Return a success banner in the password section.

### 4. Admin reset: `POST /api/users/{user_id}/password`

JSON API, admin-only (`require_admin`). Body: `{"new_password": "..."}`.

Flow:
1. Validate `new_password` (min 8 chars).
2. Hash with bcrypt; call `repo.update_password`.
3. Invalidate all sessions for that user unconditionally.
4. Return `204 No Content`.

### 5. Profile page template

Add a "Change password" card below the existing timezone/defaults sections:

```
┌─ Change password ──────────────────────────┐
│ Current password  [__________________]     │
│ New password      [__________________]     │
│                              [Save]        │
└────────────────────────────────────────────┘
```

The form POSTs to `/profile/password` via HTMX (`hx-post`, `hx-swap="outerHTML"` on the card)
and swaps in the success/error response without a full page reload.

### 6. Admin user management UI (optional, same release)

Add a **Reset password** button per user in `/admin/users` that opens an inline form
posting to `POST /api/users/{user_id}/password`.

---

## Validation rules

| Rule | Detail |
|---|---|
| `new_password` min length | 8 characters |
| `current_password` required for self-service | Reject without it; timing-safe comparison |
| Admin reset | No `current_password` check; admin privilege replaces it |
| Password same as current | Allowed (no "must differ" rule — keeps it simple) |

---

## Files touched

| File | Change |
|---|---|
| `app/presentation/routes/auth.py` | `POST /profile/password`, `POST /api/users/{user_id}/password`; login/logout session-set maintenance |
| `app/infrastructure/repositories/user_repo.py` | `update_password` method |
| `app/presentation/templates/profile.html` | Change-password card |
| `app/presentation/templates/admin/users.html` | Reset-password button + inline form (if doing the UI) |
| `tests/test_change_password.py` | New test file |
| `docs/admin-guide.md` | Document admin reset endpoint |
| `docs/api-reference.md` | Document `POST /api/users/{user_id}/password` |

---

## Edge cases

| Case | Behaviour |
|---|---|
| Wrong `current_password` | 400 with error message; no lockout (internal tool) |
| Admin resets their own password | Allowed; their current session is invalidated and they must log in again |
| User has no active sessions | `SMEMBERS` returns empty set — no-op, no error |
| `new_password` too short | 422 with validation message before any DB write |
