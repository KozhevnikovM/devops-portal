-- #510 task 5.1: the implemented environments row read (_with_usernames() phase 2, compiled by
-- SQLAlchemy) against the old join, on the same setup as join_probe.sql. Same usage:
--   docker exec -i <pg container> psql -U portal -d env_probe_496 -v n=200000 < impl_probe.sql
-- #510 username-join probe. Runs on the #496 400k dataset (env_probe_496, see
-- archive/2026-10-02-environment-list-owner-walks/probe/), inside a rolled-back transaction.
-- Usage (PostgreSQL 15 is the supported baseline; 16 was also measured):
--   docker exec -i <pg container> psql -U portal -d env_probe_496 -v n=200000 < join_probe.sql
-- The guard is spelled with an enumerated ASCII class under COLLATE "C" (no collation-dependent ranges).
-- :n extra users with random UUIDs are added. The 50 newest environments are re-pointed at random
-- ones of them, and 4 get a creator, because the seed's own 200 users have sequential ids
-- (00000000-…-0000…0199). Those sort first and hide how far a merge join walks.
\pset pager off
BEGIN;
INSERT INTO users (id, username, password_hash, role, created_at)
  SELECT gen_random_uuid(), 'probe_u' || g, 'x', 'user', now() FROM generate_series(1, :n) g;
CREATE TEMP TABLE page_ids AS SELECT id FROM environments ORDER BY created_at DESC, id DESC LIMIT 50;
UPDATE environments e
   SET user_id = (SELECT id::text FROM users WHERE username = 'probe_u' || (1 + abs(hashtext(e.id::text)) % :n))
 WHERE e.id IN (SELECT id FROM page_ids);
UPDATE environments SET created_by = (SELECT id::text FROM users WHERE username = 'probe_u7')
 WHERE id IN (SELECT id FROM page_ids LIMIT 4);
ANALYZE users; ANALYZE page_ids;

-- warm the cache
SELECT count(*) FROM environments e LEFT JOIN users u ON CAST(u.id AS VARCHAR) = e.user_id
 WHERE e.id IN (SELECT id FROM page_ids);

\echo === A: current spelling (before)
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT e.id, u.username, c.username FROM environments e
  LEFT JOIN users u ON CAST(u.id AS VARCHAR) = e.user_id
  LEFT JOIN users c ON CAST(c.id AS VARCHAR) = e.created_by
 WHERE e.id IN (SELECT id FROM page_ids);
\echo === implemented: _with_usernames() phase-2 read (after)
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT environments.id, environments.name, environments.blueprint_name, environments.user_id, environments.ttl_minutes, environments.expires_at, environments.created_by, environments.created_at, environments.construction_complete, users.username, users_1.username AS username_1 
FROM environments LEFT OUTER JOIN users ON users.id = CASE WHEN ((environments.user_id COLLATE "C") ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$') THEN CAST(environments.user_id AS UUID) END LEFT OUTER JOIN users AS users_1 ON users_1.id = CASE WHEN ((environments.created_by COLLATE "C") ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$') THEN CAST(environments.created_by AS UUID) END 
WHERE environments.id IN (SELECT id 
FROM page_ids);

\echo === implemented, generic plan
SET plan_cache_mode = force_generic_plan;
PREPARE impl AS SELECT environments.id, environments.name, environments.blueprint_name, environments.user_id, environments.ttl_minutes, environments.expires_at, environments.created_by, environments.created_at, environments.construction_complete, users.username, users_1.username AS username_1 
FROM environments LEFT OUTER JOIN users ON users.id = CASE WHEN ((environments.user_id COLLATE "C") ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$') THEN CAST(environments.user_id AS UUID) END LEFT OUTER JOIN users AS users_1 ON users_1.id = CASE WHEN ((environments.created_by COLLATE "C") ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$') THEN CAST(environments.created_by AS UUID) END 
WHERE environments.id IN (SELECT id 
FROM page_ids);

EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) EXECUTE impl;
ROLLBACK;
