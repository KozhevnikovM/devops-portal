## Context

- `users.id` is `uuid`. The references to it are `String(64)`:
  - `bookings.user_id` and `bookings.created_by`;
  - `environments.user_id` and `environments.created_by`.

  `bookings.user_id` predates authentication (migration 0001; auth arrived in 0005). Pre-auth rows hold `settings.DEV_USER_ID`, for example `dev-user`. `UserRepository.delete` removes users without touching their bookings or environments. So a reference can be a non-UUID, or the id of a user that no longer exists. Every current join is a `LEFT JOIN`, except in the two username filters, and shows no name in those cases.
- The join is written `cast(UserModel.id, String) == <ref>` in 16 places:

  | file | where |
  |---|---|
  | `booking_repo.py` | `_list_item_stmt` (owner, creator), `get` (owner, creator) |
  | `environment_repo.py` | `_with_usernames` (owner, creator), `_children`, `_children_batch`, `get` (owner, creator), `get_by_namespace` (owner, creator) |
  | `namespace_repo.py` | `held_by`, `list_held_by_username`, `list_active_not_held_by_username` |
  | `static_vm_repo.py` | `held_by` |

  The creator side uses `_CreatorUser = aliased(UserModel)` in `booking_repo` and `environment_repo`.
- The numbers behind every decision are in `measurements.md`.

## Goals / Non-Goals

**Goals:**
- Every name join is one `users_pkey` probe per reference, in custom and generic plans alike.
- Every stored value resolves exactly as it does today (spec, first requirement).
- One helper defines the conversion, and a test keeps the old spelling from coming back.

**Non-Goals:**
- Changing the column types, or adding foreign keys (Decision 3).
- An index on `bookings(user_id)` for the username filters. Their bookings side is driven by `namespace_id` and status today, and stays that way.
- The redundant `cast(BookingModel.user_id, String) == :uid` filters, which PostgreSQL folds away (`measurements.md`).
- Moving name resolution out of SQL. Every site already reads its rows in a single statement.

## Decisions

### 1. A guarded `uuid` conversion on the reference side

A new module, `app/infrastructure/repositories/_user_ref.py`:

```python
_CANONICAL_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

def user_ref_uuid(ref):
    """The users.id a stored owner/creator reference names, or NULL when it names none."""
    return case((ref.regexp_match(_CANONICAL_UUID), cast(ref, UUID(as_uuid=True))), else_=None)

def user_by_ref(user_model, ref):
    """Join condition: user_model is the user that ref names (primary-key equality)."""
    return user_model.id == user_ref_uuid(ref)
```

Every join site becomes, for example, `.join(UserModel, user_by_ref(UserModel, BookingModel.user_id), isouter=True)`.

- **Why it uses the key.** The users side is the bare `users.id`. Each outer row's value is a computable expression, so the planner can probe `users_pkey` with it in a nested loop. That is the C row of `measurements.md`: 1 user per reference, 2–3 ms for 50 rows at 20k and 200k users, also under `force_generic_plan`.
- **Why the guard is exact.** The regex is case-sensitive and anchored, and it admits exactly what `CAST(uuid AS VARCHAR)` outputs. The old condition matched a reference only when it equalled that output. The new one matches only when the reference has that output's form and parses to the same uuid. The two are the same set of matches, and the probe's truth table checks it (`dev-user`, NULL, uppercase, braced).
- **Why it cannot raise.** PostgreSQL evaluates only the selected `CASE` branch for non-constant input, so `CAST(ref AS uuid)` never sees a non-matching string. A constant reference cannot reach this path: references are always columns.
- **The SQLAlchemy spelling is pinned by a unit test.** `regexp_match` compiles to `~` on PostgreSQL. A unit test asserts the compiled SQL contains the `CASE … ~ '<pattern>' THEN CAST(… AS UUID)` shape and the users side `users.id = …`.

*Alternatives:*
- **An expression index `ON users ((CAST(id AS VARCHAR)))`** (B in `measurements.md`). It needs no query change, but the planner merge-joins against the ordered index and walks up to the largest referenced key. With random UUIDs that is most of the table: 198k index rows at 200k users, 379 ms. Bounding it needs a plan pin on every read, and it adds a second index that only works around the cast. Rejected.
- **An unguarded `CAST(ref AS uuid)`.** It errors on `dev-user`, and matches uppercase and braced references that never matched before. Rejected.
- **`ref::uuid` inside a `pg_input_is_valid` guard (PostgreSQL 16).** This would accept the non-canonical spellings, which changes which rows match. It also ties the code to PostgreSQL 16. Rejected in favour of the regex.

### 2. Username filters resolve the username to an id once

`list_held_by_username` and `list_active_not_held_by_username` drop the join, and compare `BookingModel.user_id` with a scalar subquery from a second helper in `_user_ref.py`:

```python
def user_ref_for_username(username):
    """The stored-reference form (canonical id text) of the user named username; NULL if none."""
    return select(cast(UserModel.id, String)).where(UserModel.username == username).scalar_subquery()

... .where(BookingModel.user_id == user_ref_for_username(username), ...)
```

- `users.username` is unique, so the subquery returns at most one row. PostgreSQL evaluates it once, as an InitPlan through `users_username_key`.
- Casting the user's id to text here is on the single resolved value, not on the join key. It produces the canonical form, so it compares equal exactly to the references the old join matched.
- An unknown username gives `NULL`, and `user_id = NULL` matches nothing. `list_held_by_username` therefore returns `[]`. `list_active_not_held_by_username`'s held set is empty, so it returns every active namespace. Both are today's results. The `NOT IN` keeps its `namespace_id IS NOT NULL` filter, so no NULL enters it.

*Alternative:* a separate `UserRepository.get_by_username` round trip. That costs an extra statement, and `get_by_username` filters `is_active`, which the current filter does not. Rejected.

### 3. Keep `String` references; no type migration

Moving `user_id`/`created_by` to `uuid` with foreign keys would make the join natural. But:
- the pre-auth `dev-user` rows and deleted users' references have no valid target;
- the #479 booking page indexes are built on string expressions (`'o:' || user_id || ':' || resource_type`, and so on);
- the domain carries `user_id: str` throughout.

It would be a data migration with a policy decision attached (what to do with orphan references), for no read-cost gain over Decision 1. It stays out of scope, and it can be proposed on its own.

### 4. A regression guard

A unit test scans `app/infrastructure/repositories/*.py` with `ast`, or a regex over the source. It fails on `cast(<X>.id, String)` where `<X>` is `UserModel` or an alias of it, outside `_user_ref.py`, whose `user_ref_for_username` (Decision 2) casts a single resolved id and is the only legitimate use. This mirrors how the project pins other plan-affecting spellings with unit tests.

### 5. How it is tested

- **Unit tests:**
  - the compiled SQL of `user_by_ref`;
  - the compiled SQL of each touched statement: no `CAST(users.id AS VARCHAR)` in a join condition, `users.id =` present;
  - Decision 2's subquery;
  - the regression guard.
- **Postgres integration tests.** Seed in this order: `VACUUM`, insert, commit, `VACUUM ANALYZE`, per the project's probe notes.
  - **Names.** One fixture with owner references of each kind:
    - canonical, for an existing user;
    - `dev-user`;
    - a deleted user's id;
    - uppercase;
    - NULL creator.

    Booking list, booking `get`, environment page, environment `get` and children, and both `held_by` maps return the same names as the old join, evaluated in the same test as a reference query.
  - **Plans.** With 20,000 seeded users, `EXPLAIN (ANALYZE, FORMAT JSON)` of the environments page row read and the bookings list item read must show every `users` node as an `Index Scan` on `users_pkey`, with an `Index Cond`. Each node must have `Actual Loops × Actual Rows` no greater than the page's distinct references. This is checked under `force_custom_plan` and `force_generic_plan`. A third run on the default small users table uses `SET LOCAL enable_seqscan = off`, to show the key path is available (spec: "Key lookup is available on a small users table").
  - **Username filters.** Results for a held user, a user holding nothing, and an unknown user, for both filters. The plan's `users` node is an index scan on `users_username_key`.

## Risks / Trade-offs

- **[The custom plan on a tiny users table still hashes all users.]** At 201 users it costs 1.5 ms against today's 0.65 ms. That is the planner's cost choice for a 3-page table, plus about 1 µs of regex per row. → It is accepted. The spec guarantees that the key path is available, not that it is chosen when a full read is cheaper. It flips to the key path well before the cost matters (by 20k users: 3 ms against 59 ms).
- **[A future writer stores a non-canonical reference.]** Its owner would show no name. → That is already true today. Every writer uses `str(uuid)`, which is canonical. The name-equality integration test pins the behaviour.
- **[Regex cost per row.]** It is evaluated once per row read, which is bounded by the page. It is not evaluated per user.

## Migration Plan

There is no schema change. A rollback is a code revert. Deploying is safe in any order relative to other changes.
