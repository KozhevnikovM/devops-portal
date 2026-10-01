# Environment page: measured scan costs (#496)

PostgreSQL 16.14 runs in the test container. The schema is migrated to head (0035), with the indexes the migrations create, as listed below. `jit = on` is the image default, and `work_mem` is 4MB. Every dataset is committed and then `VACUUM ANALYZE`d. Each cell is the median of three `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` runs of the statement the app runs. That statement is `_page_stmt`, compiled from the code, with page size 50 (`LIMIT 51`). The scripts are in `probe/`.

## Dataset (`probe/seed.sql`)

Each dataset has N environments, 7 minutes apart, 200 users and N standalone bookings.

| Property | Shape |
|---|---|
| Ownership | `heavy` owns 30 %. `viewer` owns 0.5 %, spread evenly. `rare` owns 20 environments, all in the oldest 5 %. The rest are spread over 195 users. |
| Dispatch | `dispatcher` dispatched 15 % on behalf of others. 0.2 % have owner = creator. |
| State | About 92 % fully released, 1 % empty (no children), and 7 % live: the newest 4 % plus 3 % scattered. |
| Children | 1–6 per environment. Live environments mix `READY`, `PROVISIONING`, `FAILED`, `QUEUED` and `RELEASED`. |
| Names | 25 % `web-…` (the dense label). `…needle…` on 0.5 % of the oldest 10 % (the sparse label). |

| N | environments | bookings | children |
|---|---|---|---|
| 20k | 20,000 | 88,899 | 68,899 |
| 100k | 100,000 | 447,548 | 347,548 |
| 400k | 400,000 | ~1.8M | ~1.4M |

Indexes in play: `ix_environments_created_at_id (created_at, id)`; `ix_bookings_environment_id (environment_id)`; and `ix_bookings_environment_id_unreleased (environment_id) WHERE status <> 'RELEASED'`. There is no index on `environments.user_id`, `created_by` or `name`.

## Where each predicate is applied today

| Predicate | How it is applied |
|---|---|
| Cursor `(created_at, id) < (…)` | `Index Cond` on `ix_environments_created_at_id`, on the index path |
| Page order | Backward walk of `ix_environments_created_at_id`, or `Seq Scan` + top-N `Sort` |
| Mine `user_id = :me OR created_by = :me` | Residual `Filter`. No index has either column. |
| Label `name ILIKE '%x%'` | Residual `Filter` |
| Released hidden | Residual `Filter` with SubPlans. Each is either a per-row `Index Only Scan` (`ix_bookings_environment_id`, and the `…_unreleased` index) or a **hashed SubPlan** that reads the whole unreleased index, or all bookings through `Seq Scan` / `Bitmap Heap Scan`. The planner chooses per run. |
| Username joins `CAST(users.id AS VARCHAR) = …` | Nested loop with a join `Filter`: page rows × users comparisons. No `users_pkey` use. |

## Page selection: custom plans (the app's normal plans)

Each cell is ms / environment rows read (returned plus removed by filter, × loops) / shared buffers. "deep" is a cursor at the middle of the scope's history. "(JIT)" marks a plan that JIT compiled. The 400k run recorded JIT function counts. At 100k, the sparse-label + hidden outliers (450–485 ms) were confirmed directly as JIT: 278 ms of compilation in a 381 ms run, and 80 ms with `jit = off`.

| Scope | Label | Released | Cursor | 20k | 100k | 400k |
|---|---|---|---|---|---|---|
| all | none | shown | first | 6.8 / 51 / 10 | 6.8 / 51 / 11 | 7.3 / 51 / 11 |
| all | none | shown | deep | 7.6 / 51 / 10 | 7.0 / 51 / 11 | 6.8 / 51 / 13 |
| all | none | hidden | first | 31.5 / 51 / 212 | 13.9 / 51 / 232 | **1001.4 / 51 / 58367** |
| all | none | hidden | deep | 31.6 / 1378 / 248 | 17.5 / 1010 / 3136 | **1014.5 / 1627 / 58411** |
| all | dense | shown | first | 6.6 / 236 / 15 | 8.5 / 175 / 15 | 7.1 / 219 / 15 |
| all | dense | hidden | deep | 36.9 / 4293 / 327 | 23.1 / 4135 / 3283 | 41.0 / 5008 / 4149 |
| all | sparse | shown | first | 13.3 / 20000 / 341 | 81.7 / 100000 / 1680 | 337.1 / 400002 / 6939 |
| all | sparse | hidden | first | 37.5 / 20000 / 625 | 452.9 / 100000 / 2985 | 539.3 / 400000 / 12176 (JIT) |
| heavy | none | shown | first | 7.9 / 186 / 14 | 6.6 / 205 / 16 | 6.4 / 129 / 14 |
| heavy | none | hidden | deep | 32.1 / 3940 / 317 | 24.3 / 4320 / 3918 | 40.0 / 4152 / 4176 |
| heavy | dense | hidden | deep | 18.2 / 10043 / 1829 | 39.0 / 17152 / 4276 | 124.1 / 15704 / 4237 |
| heavy | sparse | hidden | deep | 8.2 / 10043 / 316 | 60.9 / 51003 / 1503 | 1061.4 / 200362 / 5831 (JIT) |
| dispatcher | none | hidden | deep | 42.9 / 8472 / 439 | 20.7 / 8332 / 3981 | 39.4 / 7544 / 3943 |
| dispatcher | dense | hidden | deep | 21.3 / 9821 / 1079 | 47.0 / 32667 / 4599 | 69.6 / 36441 / 5306 |
| dispatcher | sparse | hidden | first | 35.8 / 20000 / 597 | 479.7 / 100000 / 2798 | 595.8 / 400000 / 11310 (JIT) |
| viewer | none | shown | first | 5.8 / 20000 / 341 | 10.0 / 10123 / 283 | 9.7 / 7352 / 213 |
| **viewer** | **none** | **hidden** | **first** | 10.4 / 20000 / 813 | 43.5 / 100000 / 4372 | 33.7 / 7352 / 627 |
| **viewer** | **none** | **hidden** | **deep** | 5.0 / 10231 / 438 | 23.4 / 48439 / 2202 | 101.3 / 201066 / 8858 |
| viewer | dense | hidden | first | 72.9 / 20000 / 639 | 108.5 / 100000 / 3247 | 354.6 / 400000 / 12972 (JIT) |
| viewer | sparse | hidden | first | 49.5 / 20000 / 577 | 331.8 / 100000 / 2747 | 571.7 / 400000 / 11142 (JIT) |
| rare | none | shown | first | 4.8 / 20000 / 341 | 32.7 / 100000 / 2753 | 113.3 / 400000 / 11148 |
| **rare** | **none** | **hidden** | **first** | 37.0 / 20000 / 631 | 84.4 / 100000 / 2881 | 164.2 / 400000 / 11469 (JIT) |
| rare | sparse | hidden | first | 36.6 / 20000 / 577 | 318.5 / 100000 / 2747 | 530.2 / 400000 / 11142 (JIT) |

The bold rows are the default view: Mine with released hidden and no label. The full 60-case matrix for each size is produced by `probe/run.sh`, in `out-<tag>.txt` and `results-<tag>.json`.

### Observations

1. **Mine reads everyone's history.** For low-share and rare users the plan is either `Seq Scan` + top-N `Sort` (every row, including those before the cursor) or a filtered global walk. The read grows linearly with N.
2. **The released check is unstable.** At 400k, the first run after seeding chose hashed SubPlans over `Seq Scan` / `Bitmap Heap Scan` on bookings: about 1 s and 58k buffers for a 50-row page. After autovacuum and autoanalyze, the same statement used per-row probes plus one hashed SubPlan over `ix_bookings_environment_id_unreleased`, all 101k entries: 46 ms and 425 buffers. With `work_mem = 256MB` that minimal page took 467 ms.
3. **JIT** applies to every selective plan at 400k whose estimated cost passes the thresholds. At 100k it added about 280 ms to an 80 ms query.
4. **Child loading is page-bounded at every size**: at most 3.6 ms, 201 rows and 207 buffers per page.

## Generic plans (`plan_cache_mode = force_generic_plan`)

The app runs on asyncpg, which uses prepared statements. Under `plan_cache_mode = auto`, measured after six executions, every case kept the custom plan. Forced generic plans show what happens if PostgreSQL switches:

| Scope | Label | Released | Cursor | 20k | 100k | 400k |
|---|---|---|---|---|---|---|
| all | none | shown | first | 26.9 ms / 20000 rows (Seq Scan) | 144.2 / 100000 | 515.5 / 399999 |
| all | none | hidden | first | 43.3 / 51 | 49.2 / 51 | 1583.6 / 51 |
| heavy | dense | shown | deep | 141.8 / 10043 | 688.3 / 51003 | 15.1 / 633 |

## Candidate: owner/creator walks + aggregate released check + pin (400k)

These runs add `(user_id, created_at, id)` and `(created_by, created_at, id) WHERE created_by IS NOT NULL`. Mine is a `UNION` of the two walks. "Not fully released" is spelled `(SELECT bool_and(status = 'RELEASED') FROM bookings WHERE environment_id = e.id) IS NOT TRUE`. The runs use `PREPARE` with custom and with generic plans, under the pin settings (seq/bitmap/sort/JIT off). Each cell is ms / environment rows read. (`probe/combo.py`)

| Scope | Released | No label | Dense label | Sparse label |
|---|---|---|---|---|
| all | shown | 0.1 / 51 | 0.2 / 219 | 302–331 / 369k–400k |
| all | hidden | 0.4–0.6 / 51 | 0.7 / 219 | 323–356 / 400k |
| heavy (30 %) | shown | 0.6 / 102–180 † | 1.0–2.7 / 428–842 † | 172–382 / 120k–400k † |
| heavy | hidden | 3.7–4.3 / 270 | 1.9–2.4 / 428 | 144–189 / 120k |
| dispatcher | hidden | 0.8–1.4 / 51 | 1.0–2.7 / 200 | 77–126 / 60k |
| viewer (0.5 %) | hidden | **1.0–1.6 / 51** | 9.9–12.5 / 2013 | 5.1–7.7 / 2013 |
| rare | hidden | **0.7–0.9 / 20** | 0.4–0.7 / 20 | 0.3–0.6 / 20 |

† For the heavy owner the custom plan walks `ix_environments_created_at_id` with `user_id` as a filter on the owned branch, even under the pin. Heap order follows `created_at`, so that walk is costed as cheaper. The generic plan uses the owner index.

Released-check spellings alone, 400k, first page, `jit = off` (`probe/hid.py`):

| Setting | Current `NOT EXISTS … OR EXISTS …` | `bool_and … IS NOT TRUE` | `OFFSET 0`-fenced EXISTS |
|---|---|---|---|
| defaults | 25.3 ms, hashed | 0.44 ms | 0.53 ms |
| `work_mem = 256MB` | 467.6 ms, hashed | 0.48 ms | 0.76 ms |
| Result set (whole table) | 31,310 environments | identical | identical |
