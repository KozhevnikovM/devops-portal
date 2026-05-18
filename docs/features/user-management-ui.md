# User Management UI (Issue #59)

## Goal

Give portal administrators a web UI to view all users and add new ones, without
needing to use `curl` or the raw API.

## What changes

### New routes (admin-only)

Two new routes added to `app/presentation/routes/auth.py`:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/users` | Render the user management page |
| `POST` | `/admin/users` | Create a user from the HTML form (HTMX) |

Both require `Depends(require_admin)`. Non-admin users get a 403.

### New templates

**`app/presentation/templates/admin/users.html`** — full page (extends `base.html`):
- Heading + "Add User" form
- Users table: username, role badge, active status, created date

**`app/presentation/templates/partials/user_row.html`** — single table row fragment for HTMX swap.

**`app/presentation/templates/partials/user_table.html`** — the full `<tbody>` fragment,
returned after a successful create so the list refreshes without a page reload.

### Form fields (`POST /admin/users`)

| Field | Type | Validation |
|-------|------|------------|
| `username` | string | required, unique — 409 if already taken |
| `password` | string | required |
| `role` | select: `user` \| `admin` | required |

On success: HTMX swaps the table body with the updated user list.
On duplicate username: re-render the form area with an error message (returns 200 so HTMX fires the swap).

### Navigation

`base.html`: add an **Admin** link (→ `/admin/users`) in the header, visible only when
`current_user.role == 'admin'`.

### No migration needed

The `users` table already exists.

## Files

### New
- `app/presentation/templates/admin/users.html`
- `app/presentation/templates/partials/user_table.html`

### Modified
- `app/presentation/routes/auth.py` — two new route handlers
- `app/presentation/templates/base.html` — Admin nav link for admins

## Out of scope (not in this issue)

- Deactivating / reactivating users (no API endpoint for it yet)
- Editing username, password, or role of existing users
- Deleting users
