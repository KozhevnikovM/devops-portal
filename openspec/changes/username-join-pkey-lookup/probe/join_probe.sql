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

\echo === A: current spelling, CAST(users.id AS VARCHAR) = ref
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT e.id, u.username, c.username FROM environments e
  LEFT JOIN users u ON CAST(u.id AS VARCHAR) = e.user_id
  LEFT JOIN users c ON CAST(c.id AS VARCHAR) = e.created_by
 WHERE e.id IN (SELECT id FROM page_ids);

\echo === C: users.id = guarded CAST(ref AS uuid)
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT e.id, u.username, c.username FROM environments e
  LEFT JOIN users u ON u.id = CASE WHEN e.user_id COLLATE "C" ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$'
                                   THEN CAST(e.user_id AS uuid) END
  LEFT JOIN users c ON c.id = CASE WHEN e.created_by COLLATE "C" ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$'
                                   THEN CAST(e.created_by AS uuid) END
 WHERE e.id IN (SELECT id FROM page_ids);

\echo === C, generic plan
SET plan_cache_mode = force_generic_plan;
PREPARE q(int) AS
SELECT e.id, u.username FROM environments e
  LEFT JOIN users u ON u.id = CASE WHEN e.user_id COLLATE "C" ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$'
                                   THEN CAST(e.user_id AS uuid) END
 WHERE e.id IN (SELECT id FROM page_ids LIMIT $1);
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) EXECUTE q(50);
RESET plan_cache_mode;

\echo === availability, enable_seqscan = off: A (current) vs C (guarded)
SET enable_seqscan = off;
EXPLAIN (COSTS OFF)
SELECT e.id, u.username FROM environments e
  LEFT JOIN users u ON CAST(u.id AS VARCHAR) = e.user_id
 WHERE e.id IN (SELECT id FROM page_ids);
EXPLAIN (COSTS OFF)
SELECT e.id, u.username FROM environments e
  LEFT JOIN users u ON u.id = CASE WHEN e.user_id COLLATE "C" ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$'
                                   THEN CAST(e.user_id AS uuid) END
 WHERE e.id IN (SELECT id FROM page_ids);
RESET enable_seqscan;

\echo === B: current spelling + expression index on CAST(users.id AS VARCHAR)
CREATE INDEX ix_users_id_varchar ON users ((CAST(id AS VARCHAR)));
ANALYZE users;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF)
SELECT e.id, u.username, c.username FROM environments e
  LEFT JOIN users u ON CAST(u.id AS VARCHAR) = e.user_id
  LEFT JOIN users c ON CAST(c.id AS VARCHAR) = e.created_by
 WHERE e.id IN (SELECT id FROM page_ids);

\echo === truth table: current vs guarded, for legacy and non-canonical refs
SELECT r,
       (SELECT username FROM users u WHERE CAST(u.id AS VARCHAR) = r) AS current,
       (SELECT username FROM users u WHERE u.id = CASE WHEN r COLLATE "C" ~ '^[0123456789abcdef]{8}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{4}-[0123456789abcdef]{12}$'
                                                      THEN CAST(r AS uuid) END) AS guarded
  FROM (VALUES ('dev-user'), (NULL), ('00000000-0000-0000-0000-000000000001'),
               ('00000000-0000-0000-0000-00000000000A'), ('{00000000-0000-0000-0000-000000000001}'),
               ('00000000-0000-0000-0000-00000000000٣'), ('00000000-0000-0000-0000-00000000000ä')) v(r);
ROLLBACK;
