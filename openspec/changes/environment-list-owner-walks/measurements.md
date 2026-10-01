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

The candidate adds `(user_id, created_at, id)` and `(created_by, created_at, id) WHERE created_by IS NOT NULL`, and spells "not fully released" as `(SELECT bool_and(status = 'RELEASED') FROM bookings WHERE environment_id = e.id) IS NOT TRUE`. `probe/combo.py` runs the proposed page-keys statement (design.md, Decision 1):
- All is one keyset walk.
- Mine is the owned walk and the dispatched walk, merged by `UNION ALL` + `GROUP BY (created_at, id)` + `ORDER BY` + `LIMIT 51`.
- The cursor, label and released predicates appear only when they are in effect.

Each run is one transaction. It applies the app's own `_ORDERED_WALK_SETTINGS` with `SET LOCAL`, as `_OrderedWalk` does: `enable_seqscan`, `enable_bitmapscan`, `enable_sort` and `jit` off, `enable_indexscan` on. It then `PREPARE`s the statement under `plan_cache_mode = force_custom_plan` and under `force_generic_plan`. "deep" is a cursor at the middle of the scope's history.

Each cell is ms / environment rows read, as a range over the custom and generic plans; "C / G" splits rows where the two plans differ.

**Plan shape.** These hold in all 120 runs: Mine plans are Merge Append → Group; there is no `Sort`, no hashed SubPlan, no JIT, and no `Seq Scan` or `Bitmap Heap Scan` on environments or bookings; and the cursor is an `Index Cond` wherever it is given.

| Scope | Released | Cursor | No label | Dense label | Sparse label |
|---|---|---|---|---|---|
| all | shown | first | 0.1 / 51 | 0.3 / 219 | 310.0–448.0 / C 381k / G 369k |
| all | shown | deep | 0.1–0.3 / 51 | 0.3–0.4 / 222 | 157.3–202.2 / C 181k / G 169k |
| all | hidden | first | 0.5–0.7 / 51 | 0.9–1.1 / 219 | 373.8–402.7 / 400k |
| all | hidden | deep | 10.4–12.8 / 1627 | 13.6–18.2 / 5008 | 185.6–212.9 / 200k |
| heavy (30 %) | shown | first | 0.3–0.5 / 52 | 0.4–0.9 / C 626 / G 212 † | 137.1–357.0 / C 400k / G 120k † |
| heavy (30 %) | shown | deep | 0.4–0.7 / 52 | 0.5–1.0 / C 645 / G 216 † | 61.6–192.3 / C 200k / G 60k † |
| heavy (30 %) | hidden | first | 0.9–1.0 / 52 | 1.2–1.4 / 212 | 113.0–113.7 / 120k |
| heavy (30 %) | hidden | deep | 9.5–10.0 / 1270 | 13.0–15.5 / 4852 | 58.7–60.4 / 60k |
| dispatcher | shown | first | 0.2 / 51 | 0.4 / 200 | 62.3–62.9 / 60k |
| dispatcher | shown | deep | 0.2 / 51 | 0.4 / 186 | 33.5–35.6 / 30k |
| dispatcher | hidden | first | 0.7–0.8 / 51 | 1.2 / 200 | 64.5–69.5 / 60k |
| dispatcher | hidden | deep | 8.4 / 1155 | 13.9–15.0 / 5476 | 36.5–50.0 / 30k |
| viewer (0.5 %) | shown | first | 0.2 / 51 | 0.6 / 217 | 3.6–4.0 / 2013 |
| viewer (0.5 %) | shown | deep | 0.2–0.3 / 51 | 0.5–0.6 / 186 | 2.0 / 1006 |
| viewer (0.5 %) | hidden | first | **0.7–1.0 / 51** | 7.0–8.9 / 2013 | 3.6–5.0 / 2013 |
| viewer (0.5 %) | hidden | deep | 8.4–9.2 / 1006 | 3.6–3.9 / 1006 | 2.0 / 1006 |
| rare (20 envs) | shown | first | 0.2 / 20 | 0.2 / 20 | 0.1 / 20 |
| rare (20 envs) | shown | deep | 0.2 / 9 | 0.2 / 9 | 0.1–0.2 / 9 |
| rare (20 envs) | hidden | first | **0.5–1.8 / 20** | 0.3 / 20 | 0.3–0.4 / 20 |
| rare (20 envs) | hidden | deep | 0.4 / 9 | 0.3 / 9 | 0.2 / 9 |

† The owned branch of the heavy owner's *custom* plan walks `ix_environments_created_at_id` with `user_id` as a `Filter`, under the pin. That walk is sort-free too, so the pin does not penalise it. Heap order follows `created_at`, so it is costed as cheaper. The generic plan uses the owner index, reading 212 rows for the dense label and 120k for the sparse label. That choice depends only on statistics, so it is why the spec guarantees Mine's page on every plan and its read bound only on the viewer-keyed path.

Released-check spellings alone, 400k, first page, `jit = off` (`probe/hid.py`):

| Setting | Current `NOT EXISTS … OR EXISTS …` | `bool_and … IS NOT TRUE` | `OFFSET 0`-fenced EXISTS |
|---|---|---|---|
| defaults | 25.3 ms, hashed | 0.44 ms | 0.53 ms |
| `work_mem = 256MB` | 467.6 ms, hashed | 0.48 ms | 0.76 ms |
| Result set (whole table) | 31,310 environments | identical | identical |
