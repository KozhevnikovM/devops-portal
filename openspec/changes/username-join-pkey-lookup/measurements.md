# #510 measurements

Reproduce with `probe/join_probe.sql` (command in its header). Everything runs in a rolled-back transaction. The raw, filtered output is in `probe/out.txt`.

## Databases

- **PostgreSQL 15.18**, the supported baseline: `docker-compose.yml`, `docker-compose.prod.yml` and the CI Postgres job use `postgres:15`. Container `portal-probe-pg15-510`, image `postgres:15`, database collation `en_US.utf8`. Its `users` and `environments` tables (with `vm_images` and `hw_configs`) were copied by `pg_dump` from the PostgreSQL 16 probe database, then `VACUUM ANALYZE`d. The join probe reads no other table.
- **PostgreSQL 16.14**: the #496 400k-environment dataset (`env_probe_496` in `portal-test-pg-466`), database collation `en_US.utf8`.

## Setup

- The query is the shape `list_page` step 2 uses: 50 environments read by id, plus owner and creator usernames. That is the cheapest read that carries the join. Every other join site (booking page and `get`, environment `get`/`get_by_namespace`/children, the admin `held_by` maps) joins the same way, per row it reads.
- `n` extra users with **random** UUIDs are added. The 50 page rows are re-pointed at random ones of them, and 4 get a creator. The seed's own 200 users have sequential ids (`00000000-…-0000` to `…0199`). Those sort first, which hides how far a merge join walks: an earlier run with the seed's owners showed 181 rows read where random owners read 198k.
- Each query is run warm. Timings come from `EXPLAIN ANALYZE` with timing off.

## Spellings compared

- **A, current:** `CAST(users.id AS VARCHAR) = ref`.
- **B:** A, plus an expression index `ON users ((CAST(id AS VARCHAR)))`.
- **C, proposed:** `users.id = CASE WHEN ref COLLATE "C" ~ '^[0123456789abcdef]{8}-…{12}$' THEN CAST(ref AS uuid) END`.

## Results: 50-row page, owner and creator usernames

### PostgreSQL 15.18 (baseline)

| users | A: users read, time | B: users read, time | C: users read, time | C generic plan |
|---|---|---|---|---|
| 201 | 2 × seq 201, 0.56 ms | seq 201 + key probe, 0.63 ms | pkey 1 × 50 + 0 × 2, 0.77 ms | pkey 1 × 50, 0.71 ms |
| 20,200 | 2 × seq 20,200, 69 ms | merge: 20,195 + 1,401 index rows, 21 ms | pkey 1 × 50 + 1 × 3, 1.2 ms | pkey 1 × 50, 0.78 ms |
| 200,200 | 2 × seq 200,200, 1,030 ms | merge: 199,948 + 181,524 index rows, 431 ms | pkey 1 × 50 + 1 × 3, 1.2 ms | pkey 1 × 50, 0.83 ms |

### PostgreSQL 16.14

| users | A: users read, time | B: users read, time | C: users read, time | C generic plan |
|---|---|---|---|---|
| 201 | 2 × seq 201 (hash), 0.87 ms | 2 × seq 201, 1.0 ms | 2 × seq 201 (hash), 1.7 ms | pkey 1 × 50, 0.84 ms |
| 20,200 | 2 × seq 20,200, 68 ms | merge: 20,044 + 7,247 index rows, 25 ms | pkey 1 × 50 + 1 × 3, 2.7 ms | pkey 1 × 50, 1.8 ms |
| 200,200 | 2 × seq 200,200, 1,318 ms | merge: 190,800 + 139,953 index rows, 361 ms | pkey 1 × 50 + 1 × 3, 2.2 ms | pkey 1 × 50, 1.8 ms |

- **A** reads the whole users table twice per page, once per joined alias. Its cost grows linearly with users.
- **B** makes the join usable by an index, but the planner merge-joins against the ordered expression index. It walks the index up to the largest referenced key, which with random UUIDs is most of the table. Its read is no more bounded by the page than A's.
- **C** probes `users_pkey` once per row read. On PostgreSQL 15 it does so in every run, including 201 users. On PostgreSQL 16 the custom plan prefers hashing the 201-row table (1.7 ms against A's 0.87 ms), which is a cost choice for a 3-page table, and it probes from 20k users up. Generic plans probe at every size on both versions. The per-row regex costs about 1 µs.
- Example `Index Cond` (PostgreSQL 15): `(id = CASE WHEN ((e.user_id)::text ~ '^[0123456789abcdef]{8}-…$'::text) THEN (e.user_id)::uuid ELSE NULL::uuid END)`. `EXPLAIN` does not print `COLLATE "C"`, because the planner folds a `CollateExpr` into the operator's input collation. `pg_get_viewdef` of the same expression shows `((e.user_id)::text COLLATE "C") ~ …`, so the clause is kept.

## Index-path availability (sequential scans disabled)

`SET enable_seqscan = off`, 50-row page, owner name. Measured on both versions at n = 1, 20,000 and 200,000:

| spelling | users node |
|---|---|
| A, current | `Seq Scan on users u`. The condition is not sargable, so there is no index path to switch to. |
| C, proposed | `Index Scan using users_pkey on users u`, with the reference as the `Index Cond` |

This is the property the spec makes normative. Which plan the planner picks with seq scans enabled is the measured behaviour in the tables above, and it is not promised.

## Truth table

Is the user found for each `ref` value? The results are the same on 15.18 and 16.14.

| ref | A | C |
|---|---|---|
| canonical lowercase UUID of a user | found | found |
| `dev-user` (pre-auth `DEV_USER_ID` rows) | none | none, no error |
| `NULL` (no creator) | none | none |
| UUID with uppercase hex | none | none |
| `{…}`-braced UUID | none | none |
| canonical layout ending in `٣` (Arabic-Indic digit) | none | none, no error |
| canonical layout ending in `ä` | none | none, no error |

The guard admits exactly the strings that `CAST(uuid AS VARCHAR)` can produce: lowercase, hyphenated, 36 ASCII characters. So C matches the same rows as A. A bare `CAST(ref AS uuid)` would instead raise an error on `dev-user`, and would match the uppercase and braced forms that A does not.

## Collation

PostgreSQL documents regex bracket ranges as collation-dependent, so the guard enumerates its class (`[0123456789abcdef]`) and matches under `COLLATE "C"`. The range form `[0-9a-f]` was also checked, for each single character in `g E Ａ ٣ ５ ａ é ² ß ä ⓐ ½ Ⅲ 𝟑`, under these collations:
- `default` (`en_US.utf8`);
- `C`;
- the ICU collations `und-x-icu`, `en-US-x-icu`, `tr-TR-x-icu`, `sv-SE-x-icu` and `da-DK-x-icu`.

On both 15.18 and 16.14, none of those characters passed the range form or the enumerated form. In practice, today's engine evaluates these ranges by code point. The enumerated `COLLATE "C"` spelling makes the guard independent of that implementation detail.

## Username-filter reads

`namespace_repo.list_held_by_username` and `list_active_not_held_by_username` filter on `users.username = :u` through the same cast join. There the user side is fixed, and the join is wasted work: the username's id can be resolved once, through `users_username_key`, and compared to `bookings.user_id` as a plain string. That replaces a join to all users with a single unique-key lookup. These two are covered by unit and integration tests in the change, not by this probe. `bookings` has no index led by `user_id` alone, so the bookings side of these reads is unchanged.

## Not affected

`cast(BookingModel.user_id, String) == :uid` (`booking_repo.get_live_standalone_namespace_booking`, `namespace_repo.list_held_standalone_by_user`) casts the string column to its own type. PostgreSQL folds that cast to `(user_id)::text = …`, and the plan shows it as an ordinary filter. It is redundant but harmless, so it is out of scope.
