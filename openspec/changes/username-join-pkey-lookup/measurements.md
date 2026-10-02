# #510 measurements

Reproduce with `probe/join_probe.sql` (command in its header). It runs on the #496 400k-environment dataset (`env_probe_496` in the test Postgres container, PostgreSQL 16.14). Everything happens in a rolled-back transaction. The raw output is in `probe/out.txt`.

## Setup

- The query is the shape `list_page` step 2 uses: 50 environments read by id, plus owner and creator usernames. That is the cheapest read that carries the join. Every other join site (booking page and `get`, environment `get`/`get_by_namespace`/children, the admin `held_by` maps) joins the same way, per row it reads.
- `n` extra users with **random** UUIDs are added. The 50 page rows are re-pointed at random ones of them, and 4 get a creator. The seed's own 200 users have sequential ids (`00000000-…-0000` to `…0199`). Those sort first, which hides how far a merge join walks: an earlier run with the seed's owners showed 181 rows read where random owners read 198k.
- Each query is run warm. Timings come from `EXPLAIN ANALYZE` with timing off.

## Spellings compared

- **A, current:** `CAST(users.id AS VARCHAR) = ref`.
- **B:** A, plus an expression index `ON users ((CAST(id AS VARCHAR)))`.
- **C:** `users.id = CASE WHEN ref ~ '<canonical lowercase uuid>' THEN CAST(ref AS uuid) END`.

## Results: 50-row page, owner and creator usernames

| users | A: users read, time | B: users read, time | C: users read, time | C generic plan |
|---|---|---|---|---|
| 201 | 2 × seq 201 (hash), 0.65 ms | 2 × seq 201, 0.68 ms | 2 × seq 201 (hash), 1.5 ms | pkey 1 × 50, 0.74 ms |
| 20,200 | 2 × seq 20,200, 59 ms | merge: 19,984 + 12,551 index rows, 31 ms | pkey 1 × 50 + 1 × 3, 3.1 ms | pkey 1 × 50, 1.8 ms |
| 200,200 | 2 × seq 200,200, 859 ms | merge: 198,231 + 162,383 index rows, 379 ms | pkey 1 × 50 + 1 × 3, 2.2 ms | pkey 1 × 50, 2.0 ms |

- **A** reads the whole users table twice per page, once per joined alias, and the cost grows linearly with users.
- **B** makes the join usable by an index, but the planner merge-joins against the ordered expression index. It walks the index up to the largest referenced key, which with random UUIDs is most of the table. The read is no more bounded by the page than A's. The plan could be forced to a nested loop, but only with a session pin, and the read is still on a second index of `users` that exists only to work around the cast.
- **C** probes `users_pkey` once per row read, in custom and generic plans alike, from 20k users up. At 201 users the custom plan prefers hashing the 201-row table (1.5 ms against A's 0.65 ms). That is the planner's correct cost choice for a table that fits in 3 pages, and it is not a page-bound concern. The per-row regex costs about 1 µs.

## Truth table

Is the user found for each `ref` value?

| ref | A | C |
|---|---|---|
| canonical lowercase UUID of a user | found | found |
| `dev-user` (pre-auth `DEV_USER_ID` rows) | none | none, no error |
| `NULL` (no creator) | none | none |
| UUID with uppercase hex | none | none |
| `{…}`-braced UUID | none | none |

The guard admits exactly the strings that `CAST(uuid AS VARCHAR)` can produce: lowercase, hyphenated, 36 characters. So C matches the same rows as A. A bare `CAST(ref AS uuid)` would instead raise an error on `dev-user`, and would match the uppercase and braced forms that A does not.

## Username-filter reads

`namespace_repo.list_held_by_username` and `list_active_not_held_by_username` filter on `users.username = :u` through the same cast join. There the user side is fixed, and the join is wasted work: the username's id can be resolved once, through `users_username_key`, and compared to `bookings.user_id` as a plain string. That replaces a join to all users with a single unique-key lookup. These two are covered by unit and integration tests in the change, not by this probe. `bookings` has no index led by `user_id` alone, so the bookings side of these reads is unchanged.

## Not affected

`cast(BookingModel.user_id, String) == :uid` (`booking_repo.get_live_standalone_namespace_booking`, `namespace_repo.list_held_standalone_by_user`) casts the string column to its own type. PostgreSQL folds that cast to `(user_id)::text = …`, and the plan shows it as an ordinary filter. It is redundant but harmless, so it is out of scope.
