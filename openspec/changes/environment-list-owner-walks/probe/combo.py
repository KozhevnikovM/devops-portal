from sqlalchemy import create_engine
c = create_engine("postgresql+psycopg2://portal:portal@localhost:5433/env_probe_496", isolation_level="AUTOCOMMIT").raw_connection()
c.autocommit = True; c = c.cursor()
c.execute("CREATE INDEX IF NOT EXISTS cand_env_owner_page ON environments (user_id, created_at, id)")
c.execute("CREATE INDEX IF NOT EXISTS cand_env_creator_page ON environments (created_by, created_at, id) WHERE created_by IS NOT NULL")
c.execute("ANALYZE environments")
HID = "(SELECT bool_and(b.status = 'RELEASED') FROM bookings b WHERE b.environment_id = e.id) IS NOT TRUE"
def branch(scope, hidden):
    w = [scope, "e.name ILIKE $2", "(e.created_at, e.id) < ($3, $4)"]
    if hidden: w.append(HID)
    return f"SELECT e.created_at, e.id FROM environments e WHERE {' AND '.join(w)} ORDER BY e.created_at DESC, e.id DESC LIMIT $5"
def walk(n, o): o.append(n); [walk(x, o) for x in n.get("Plans", [])]; return o
U = {"all": None, "heavy": "1", "dispatcher": "2", "rare": "3", "viewer": "4"}
for mode in ("force_custom_plan", "force_generic_plan"):
    for who, n in U.items():
        for hidden in (False, True):
            if n is None:
                q = branch("true", hidden)
            else:
                q = (f"SELECT * FROM (({branch('e.user_id = $1', hidden)}) UNION ({branch('e.created_by = $1', hidden)})) k "
                     f"ORDER BY k.created_at DESC, k.id DESC LIMIT $5")
            c.execute("DEALLOCATE ALL; RESET ALL; SET jit = off; SET plan_cache_mode = " + mode)
            c.execute("PREPARE p(text, text, timestamptz, uuid, int) AS " + q)
            uid = "00000000-0000-0000-0000-" + (n or "0").zfill(12)
            for label in ("%", "%web%", "%needle%"):
                ts = []
                for _ in range(3):
                    c.execute(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) EXECUTE p('{uid}', '{label}', 'infinity', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 51)")
                    p = c.fetchone()[0][0]; ts.append(p["Execution Time"])
                env = [x for x in walk(p["Plan"], []) if x.get("Relation Name") == "environments"]
                read = sum((x["Actual Rows"] + x.get("Rows Removed by Filter", 0)) * x["Actual Loops"] for x in env)
                print(f"{mode[6:12]:6} {who:10} {'hidden' if hidden else 'shown':6} {label:9} ms={sorted(ts)[1]:8.2f} buf={p['Plan'].get('Shared Hit Blocks',0):6} envread={read:7} idx={','.join(x.get('Index Name', x['Node Type']) for x in env)}")
