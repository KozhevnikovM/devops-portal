-- Seed a skewed environments dataset of :n environments (psql -v n=...).
-- Users: :u users. heavy = u001 (30% of envs), dispatcher = u002 (creates 15%, on behalf of others),
-- rare = u003 (20 envs, all in the oldest 5%), viewer = u004 (~0.5%, uniform).
-- ~92% fully released, ~1% empty (no children), ~7% live (newest 4% + 3% scattered older).
-- Names: 25% start with 'web-' (dense label), 'needle' on ~0.05% in the oldest 10% (sparse label).
TRUNCATE booking_audit, vms, bookings, environments, users CASCADE;

INSERT INTO users (id, username, password_hash, role)
SELECT ('00000000-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid, 'u' || lpad(g::text, 5, '0'), 'x', 'user'
FROM generate_series(0, :u - 1) g;

CREATE TEMP TABLE e AS
SELECT g AS n,
       gen_random_uuid() AS id,
       timestamptz '2024-01-01' + (g * interval '7 minutes') AS created_at,
       random() AS r_owner, random() AS r_disp, random() AS r_state, random() AS r_name,
       1 + floor(random() * 6)::int AS kids
FROM generate_series(1, :n) g;

INSERT INTO environments (id, name, blueprint_name, user_id, ttl_minutes, expires_at, created_at, created_by, construction_complete)
SELECT id,
       CASE WHEN n <= :n * 0.10 AND r_name < 0.005 THEN 'old-needle-' || n
            WHEN r_name < 0.25 THEN 'web-' || n
            WHEN r_name < 0.50 THEN 'db-' || n
            ELSE 'stack-' || n END,
       'bp',
       owner,
       60, timestamptz '2999-01-01', created_at,
       CASE WHEN r_disp < 0.15 THEN '00000000-0000-0000-0000-000000000002'
            WHEN r_disp < 0.152 THEN owner          -- owner = creator (self-dispatch)
            ELSE NULL END,
       true
FROM (
  SELECT e.*,
    CASE WHEN r_owner < 0.30 THEN '00000000-0000-0000-0000-000000000001'
         WHEN r_owner < 0.305 THEN '00000000-0000-0000-0000-000000000004'
         ELSE '00000000-0000-0000-0000-' || lpad((5 + floor(random() * (:u - 5)))::int::text, 12, '0') END AS owner
  FROM e) x;

-- rare user: 20 envs among the oldest 5%
UPDATE environments SET user_id = '00000000-0000-0000-0000-000000000003', created_by = NULL
WHERE id IN (SELECT id FROM e WHERE n <= :n * 0.05 ORDER BY r_owner LIMIT 20);

-- state: 'empty' 1%, 'live' (newest 4% or 3% scattered), else 'released'
CREATE TEMP TABLE es AS
SELECT e.id, e.kids, e.created_at, env.user_id, env.created_by,
  CASE WHEN r_state < 0.01 THEN 'empty'
       WHEN n > :n * 0.96 OR r_state > 0.97 THEN 'live'
       ELSE 'released' END AS state
FROM e JOIN environments env USING (id);

INSERT INTO bookings (id, user_id, status, ttl_minutes, expires_at, created_at, resource_type, environment_id, created_by, drive_type, config_roles, extra_vars)
SELECT gen_random_uuid(), es.user_id,
  CASE WHEN es.state = 'released' THEN 'RELEASED'
       WHEN k = 1 THEN (ARRAY['READY','PROVISIONING','FAILED','QUEUED'])[1 + floor(random()*4)::int]
       ELSE (ARRAY['READY','RELEASED','READY'])[1 + floor(random()*3)::int] END,
  60, timestamptz '2999-01-01', es.created_at + k * interval '1 second',
  (ARRAY['VM','NAMESPACE','STATIC_VM'])[1 + floor(random()*3)::int], es.id, es.created_by, 'HDD', '[]', '{}'
FROM es CROSS JOIN LATERAL generate_series(1, es.kids) k
WHERE es.state <> 'empty';

-- standalone bookings (not in any environment), same count as environments, mostly released
INSERT INTO bookings (id, user_id, status, ttl_minutes, expires_at, created_at, resource_type, drive_type, config_roles, extra_vars)
SELECT gen_random_uuid(), '00000000-0000-0000-0000-' || lpad(floor(random()*:u)::int::text, 12, '0'),
  CASE WHEN random() < 0.93 THEN 'RELEASED' ELSE 'READY' END, 60, timestamptz '2999-01-01',
  timestamptz '2024-01-01' + (g * interval '7 minutes'), 'VM', 'HDD', '[]', '{}'
FROM generate_series(1, :n) g;

VACUUM ANALYZE users;
VACUUM ANALYZE environments;
VACUUM ANALYZE bookings;
SELECT (SELECT count(*) FROM environments) envs, (SELECT count(*) FROM bookings) bookings,
       (SELECT count(*) FROM bookings WHERE environment_id IS NOT NULL) children;
