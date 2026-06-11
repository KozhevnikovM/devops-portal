# Feature: Dispatcher UI & docs (v0.9.0 P3, #231)

## Goal

Make the **`dispatcher`** role (data + API from #229/#230) usable and visible in the **browser UI**, and
document it fully. Three threads: (1) admins can create dispatchers from the user-management page;
(2) booking/environment rows show *who a resource was dispatched by* and let the creating dispatcher
manage it from the UI (closing the gap left by #230, which only changed the API/use-case layer);
(3) the admin guide and API reference get a complete dispatcher section.

## Domain model — resolve `created_by` to a username (display only)

`created_by` stores the acting dispatcher's **id** (#229). For a human-readable "via <dispatcher>"
marker the UI needs the **username**. Add a derived, read-only field — no schema change:

- `Booking.created_by_username: str | None` and `Environment.created_by_username: str | None`
  (alongside the existing `owner_username`).
- The repos already left-join `UserModel` for the owner; add a **second aliased left-join** on
  `cast(UserModel.id, String) == <table>.created_by` to populate `created_by_username`. Applies to
  `BookingRepository` (`list_all`/`list_by_user`/`get`) and `EnvironmentRepository` (`_list`/`get`).
  `NULL` `created_by` → `NULL` username (a plain self-order shows no marker).

## What changes

### 1. Admin user management — create a dispatcher
- `admin/users.html`: the **Role** `<select>` gains `<option value="dispatcher">dispatcher</option>`.
- `partials/user_table.html`: render a distinct **dispatcher** role badge (e.g. purple) — today only
  `admin` (orange) and the `else→user` (blue) are styled; make it an explicit three-way so
  `dispatcher` is labelled, not mislabelled as "user".
- **Server-side validation** (defence in depth): both `POST /api/users` and `POST /admin/users`
  reject a role not in `{user, admin, dispatcher}` → `400` (API) / inline error (UI). Today the role
  is an unvalidated free string.

### 2. Rows — show the dispatcher + let it manage
- `partials/booking_row.html` & `partials/environment_row.html`: next to the owner, when
  `created_by_username` is set, show a subtle **"via {{ created_by_username }}"** marker.
- Management buttons (release / extend / release-environment) are currently gated on
  `is_owner or admin`. Introduce a template-level **`can_manage`** =
  `is_owner or current_user.role == 'admin' or booking.created_by == (current_user.id | string)`,
  matching the backend `can_manage` from #230, so the **creating dispatcher** sees the same
  release/extend affordances in the browser for resources it dispatched.
- **Credentials stay owner+admin only.** `can_see_creds` is *not* broadened — the dispatcher already
  received one-time credentials in the order response (#229); the list/row never re-vends secrets to
  a non-owner (keeps the #137 secret-minimisation rule intact). The dispatcher manages the
  *lifecycle*, not credential re-display.

### 3. Docs
- **`docs/admin-guide.md`** — promote the brief #229 note to a full **Dispatcher role** section:
  create a dispatcher user (UI dropdown + `POST /api/users`), mint its API token, how `on_behalf_of`
  works end-to-end for a CI pipeline, and the visibility/management rules (owner + creating dispatcher
  + admin) from #230.
- **`docs/api-reference.md`** — a concise **permission rules** summary for bookings/environments:
  who can list, read, release, extend (owner, creating dispatcher, admin) — consolidating the notes
  added piecemeal in #229/#230.

## Edge cases / non-goals
- **No schema change, no migration** — `created_by_username` is a join, display-only.
- A self-order (`created_by` null) shows **no** "via" marker and behaves exactly as before.
- The dispatcher badge/option does not grant anything by itself — authorization is the role checks
  already shipped in #229/#230. This item is presentation + docs + role-input validation.
- No per-dispatcher allow-list of targets (still any active user — future, per #227).

## Tests
- `created_by_username` is populated by the repo join (owned + dispatched rows) and `NULL` for a
  self-order.
- `admin/users` + `POST /api/users`: `dispatcher` is accepted; an invalid role → `400`/inline error.
- Row rendering: a dispatched booking/environment shows the "via <dispatcher>" marker; a self-order
  does not.
- Row rendering: the creating dispatcher gets release/extend buttons for a dispatched resource; an
  unrelated user does not; credentials are **not** shown to the dispatcher in the row.
- User-table badge: a dispatcher user renders the dispatcher badge (not the user badge).
