from sqlalchemy import create_engine
c = create_engine("postgresql+psycopg2://portal:portal@localhost:5433/env_probe_496").raw_connection().cursor()
V = {
 "current": "(NOT EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id) OR EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id AND b.status <> 'RELEASED'))",
 "bool_and": "(SELECT bool_and(b.status = 'RELEASED') FROM bookings b WHERE b.environment_id = e.id) IS NOT TRUE",
 "offset0": "(NOT EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id OFFSET 0) OR EXISTS (SELECT 1 FROM bookings b WHERE b.environment_id = e.id AND b.status <> 'RELEASED' OFFSET 0))",
}
cases = {"all-first": "", "viewer-all-history": "AND e.user_id = '00000000-0000-0000-0000-000000000004'"}
for settings in ("", "SET work_mem='256MB';", "SET work_mem='256MB'; SET enable_indexscan=off; SET enable_indexonlyscan=off;"):
    for vn, pred in V.items():
        for cn, extra in cases.items():
            c.execute("RESET ALL; SET jit=off; " + settings)
            q = f"SELECT e.id FROM environments e WHERE {pred} {extra} ORDER BY e.created_at DESC, e.id DESC LIMIT 51"
            ts = []
            for _ in range(3):
                c.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + q); p = c.fetchone()[0][0]; ts.append(p["Execution Time"])
            txt = str(p)
            print(f"{settings[:40]:40} {vn:9} {cn:20} ms={sorted(ts)[1]:8.2f} buf={p['Plan'].get('Shared Hit Blocks',0)+p['Plan'].get('Shared Read Blocks',0):6} hashed={'hashed SubPlan' in txt} seqbk={'Seq Scan' in txt}")
# correctness: all three agree on the full set
c.execute("RESET ALL")
sets = {}
for vn, pred in V.items():
    c.execute(f"SELECT count(*), sum(hashtext(e.id::text)::bigint) FROM environments e WHERE {pred}"); sets[vn] = c.fetchone()
print(sets)
