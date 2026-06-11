# Feature: Dispatcher visibility & management (v0.9.0 P2, #230)

## Goal

After #229 a dispatcher can **order** resources on behalf of another user (`user_id` = the target,
`created_by` = the dispatcher). But the dispatcher then **loses sight** of what it dispatched: listing
is owner-scoped (`user_id == self`), and release/extend require ownership. A CI dispatcher needs to
**see and manage** the bookings/environments it created for others — without becoming an admin.

This item makes `created_by` confer visibility and management rights, everywhere a booking or
environment is listed, released, or extended (JSON API **and** the browser pages).

## Domain model — no schema change

No migration. Builds entirely on `bookings.created_by` / `environments.created_by` from #229.

Two predicates, applied uniformly:

- **Visible to user X** ⟺ `user_id == X` **OR** `created_by == X`. (Admins see everything.)
- **Manageable by user X** ⟺ `user_id == X` **OR** `created_by == X` **OR** X is `admin`.

`created_by` is only ever set to a dispatcher/admin id, so a normal user never matches another
booking's `created_by` — the broadened rule is a no-op for ordinary users and needs **no role
branch**. A dispatcher naturally sees/manages its own bookings *and* everything it dispatched.

## What changes

### Repositories — broaden the owner-scoped queries
- `BookingRepository.list_by_user(session, user_id, ...)` — WHERE becomes
  `user_id == X OR created_by == X` (was `user_id == X`). Same for
- `EnvironmentRepository.list_by_user`. Docstrings updated to "visible to user" semantics. All
  existing callers (browser `bookings.py` / `environments.py`, the two JSON list endpoints) inherit
  the broadened scope for free; admin paths (`list_all`) are untouched.

### Application — a shared management predicate
- New `app/application/use_cases/_permissions.py`:
  ```python
  def can_manage(*, owner_id: str, created_by: str | None, user: User) -> bool:
      return user.role == "admin" or str(user.id) in {owner_id, created_by}
  ```
- `ReleaseBookingUseCase.execute` — replace the inline
  `booking.user_id != id and role != admin` check with `can_manage(...)`.
- `ExtendBookingUseCase.execute` — same. **Note:** extend is currently *owner-only* (admins can't
  extend); switching to `can_manage` aligns it with release by also letting **admin** and the
  **creating dispatcher** extend. This is the intended P2 behaviour (consistent management rights).
- `ReleaseEnvironmentUseCase.execute` — use `can_manage` for the environment.

### Presentation — the per-resource GET guards
- `GET /api/bookings/{id}/audit`, `GET /api/environments/{id}`, and the browser
  `GET /environments/{id}/row` / booking audit guards currently read
  `resource.user_id != id and role != admin` → switch to `can_manage(...)` so a dispatcher can read
  the detail/audit of what it dispatched.

### Serialization — already done
`created_by` is already in `_summary` (#229) and `_serialize` (#229); no change. (Rendering it as a
human "via <dispatcher>" marker in the UI is **#231**, not here.)

## Edge cases / non-goals
- **No new fields, no migration** — pure authorization/query broadening.
- A dispatcher still **cannot** see or manage resources it neither owns nor created (no org-wide
  visibility — that's admin).
- The **owner** keeps full rights over their own resource even though a dispatcher created it (both
  match the predicate).
- Quota/attribution is unchanged (set at order time in #229).
- UI affordances (showing "via dispatcher", the role dropdown) are **#231**.

## Tests
- Repo: `list_by_user` returns rows where `created_by == X` even when `user_id != X`; excludes rows
  where neither matches. Same for environments.
- `can_manage`: true for owner, for creating dispatcher, for admin; false for an unrelated user.
- API: dispatcher `GET /api/bookings` includes a booking it dispatched (owner is someone else);
  an unrelated user's list excludes it.
- API: dispatcher releases / extends a booking it dispatched → 200/202; unrelated user → 403; owner
  still → 200/202.
- Environment: dispatcher lists + releases an environment it dispatched; `GET /api/environments/{id}`
  detail readable by the creating dispatcher, 403 for an unrelated user.
