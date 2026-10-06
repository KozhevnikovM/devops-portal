## Why

#510 was found during #496. Every read that shows an owner or creator name joins `users` on `CAST(users.id AS VARCHAR) = <ref>`. `users.id` is a UUID, while the referencing columns (`bookings.user_id`/`created_by`, `environments.user_id`/`created_by`) are strings. Casting the key side means `users_pkey` cannot serve the join. Each joined alias therefore reads the whole `users` table, whatever page bound the rest of the read has.

The measurements are in `measurements.md`. They were taken on PostgreSQL 15.18, the supported baseline used by compose, prod and CI, and repeated on 16.14. For a 50-row environments page with owner and creator names, the current join reads all users twice:
- 0.56 ms at 200 users;
- 69 ms at 20k users;
- 1,030 ms at 200k users.

A guarded primary-key comparison makes a per-row `users_pkey` lookup available. The current spelling has no index path even with sequential scans disabled. Where the planner takes that lookup, it reads one user per row: about 0.8–1.2 ms at every size on PostgreSQL 15, in custom and generic plans. The cost is small today, but it grows with users × rows read, independently of the page bounds #479 and #496 established.

## What Changes

- **Username joins compare on the users primary key.** Every owner or creator username join uses `users.id = <ref as uuid>`, through one shared SQL helper. The conversion is guarded so that it applies only to the canonical lowercase UUID text that `CAST(uuid AS VARCHAR)` produces. The guard is an enumerated ASCII class matched under `COLLATE "C"`, so no collation can change what it admits. Any other value resolves to no user, exactly as today, and never raises an error. That covers legacy pre-auth values such as `dev-user`, NULL, and non-canonical or non-ASCII spellings. The join sites are:
  - the bookings list item and `get`;
  - the environments page rows, `get`, `get_by_namespace`, `_children` and `_children_batch`;
  - the admin `held_by` maps for namespaces and static VMs.
- **Username filters resolve the username once.** `list_held_by_username` and `list_active_not_held_by_username` (the `username` / `not_username` filters of `GET /api/v1/namespaces`) compare `bookings.user_id` with the user's id. That id is read once, by its unique username, instead of joining every user.
- **A guard against regressions.** A unit test fails if any repository joins `users` through `CAST(users.id AS VARCHAR)` again.
- **Unchanged:**
  - every displayed name;
  - which rows any read returns, including rows whose owner was deleted, and legacy non-UUID owners, which still show no name;
  - the column types, all existing indexes, the API, the templates and the page contracts.

  No migration and no new index.

## Capabilities

### New Capabilities

- `user-name-resolution`: how reads resolve a stored owner or creator reference to a username. The join can always be resolved by a primary-key lookup per row read; the planner's choice is not promised. Every stored value resolves as it does today, independently of collation, including references that are not users. A username filter resolves the username once.

### Modified Capabilities

_None._ `booking-listing` and `environment-listing` keep their requirements. Their per-row lookup bounds already hold; this change removes a per-page cost proportional to the size of `users` that sits underneath them.

## Impact

- **Code:**
  - a new helper module under `app/infrastructure/repositories/`;
  - its call sites in `booking_repo.py`, `environment_repo.py`, `namespace_repo.py` and `static_vm_repo.py`.
- **Tests:**
  - unit tests of the compiled join and filter SQL, and the regression guard;
  - Postgres integration tests:
    - availability: with sequential scans off, users are read through `users_pkey` with an `Index Cond`, in custom and generic plans;
    - a measured regression on PostgreSQL 15, not a guarantee: with 20k users, the planner's own plan is the per-row probe;
    - name equality against the current spelling for canonical, legacy, NULL, deleted-owner, non-canonical and non-ASCII references, including under an ICU collation;
    - the username filters' results unchanged.
- **Not affected:**
  - the redundant `cast(BookingModel.user_id, String) == :uid` filters, which PostgreSQL folds to a plain text comparison (`measurements.md`);
  - migrating `user_id`/`created_by` to `uuid` with foreign keys, which is rejected in `design.md`.
- **Tracking:** closing #510 completes the #496 follow-ups listed in #498 and #438.
