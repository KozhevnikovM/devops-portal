"""Candidate page-keys query (#496, design.md Decisions 1-3), measured as the app would run it.

- Plan settings are the app's own `_ORDERED_WALK_SETTINGS` (seq/bitmap scans off, sorts off,
  index scans on, JIT off), applied with SET LOCAL inside a transaction like `_OrderedWalk`.
- Mine is two keyset walks merged by UNION ALL + GROUP BY (created_at, id) + ORDER BY + LIMIT;
  All is one walk. Optional predicates (cursor, label, released) appear only when in effect.
- Each statement is PREPAREd and run with plan_cache_mode force_custom_plan and
  force_generic_plan.

Needs the two candidate indexes (created here if missing) on a seeded `env_probe_496`.
"""
import statistics

from sqlalchemy import create_engine

from app.infrastructure.repositories._ordered_walk import _ORDERED_WALK_SETTINGS

URL = "postgresql+psycopg2://portal:portal@localhost:5433/env_probe_496"
LIMIT = 50
USERS = {"all": None, "heavy": "1", "dispatcher": "2", "rare": "3", "viewer": "4"}
LABELS = {"none": None, "dense": "web", "sparse": "needle"}
NOT_FULLY_RELEASED = (
    "(SELECT bool_and(b.status = 'RELEASED') FROM bookings b WHERE b.environment_id = e.id) IS NOT TRUE"
)


def walk_sql(scope: str | None, *, label: bool, hidden: bool, cursor: bool) -> str:
    """One keyset walk: $1 viewer, $2 label pattern, $3/$4 cursor, $5 limit + 1."""
    where = [] if scope is None else [f"e.{scope} = $1"]
    if cursor:
        where.append("(e.created_at, e.id) < ($3, $4)")
    if label:
        where.append("e.name ILIKE $2")
    if hidden:
        where.append(NOT_FULLY_RELEASED)
    w = f"WHERE {' AND '.join(where)} " if where else ""
    return f"SELECT e.created_at, e.id FROM environments e {w}ORDER BY e.created_at DESC, e.id DESC LIMIT $5"


def keys_sql(mine: bool, **kw) -> str:
    if not mine:
        return walk_sql(None, **kw)
    return (
        f"SELECT created_at, id FROM (({walk_sql('user_id', **kw)}) UNION ALL ({walk_sql('created_by', **kw)})) k "
        "GROUP BY created_at, id ORDER BY created_at DESC, id DESC LIMIT $5"
    )


def nodes(n, out):
    out.append(n)
    for c in n.get("Plans", []):
        nodes(c, out)
    return out


def main():
    conn = create_engine(URL).raw_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE INDEX IF NOT EXISTS cand_env_owner_page ON environments (user_id, created_at, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS cand_env_creator_page ON environments (created_by, created_at, id) "
                "WHERE created_by IS NOT NULL")
    cur.execute("ANALYZE environments")
    conn.autocommit = False
    print("settings:", _ORDERED_WALK_SETTINGS)
    for mode in ("force_custom_plan", "force_generic_plan"):
        for who, n in USERS.items():
            uid = "00000000-0000-0000-0000-" + (n or "0").zfill(12)
            cur.execute(
                "SELECT created_at, id FROM environments"
                + ("" if n is None else f" WHERE user_id = '{uid}' OR created_by = '{uid}'")
                + " ORDER BY created_at DESC, id DESC OFFSET (SELECT count(*) / 2 FROM environments"
                + ("" if n is None else f" WHERE user_id = '{uid}' OR created_by = '{uid}'")
                + ") LIMIT 1"
            )
            mid = cur.fetchone()
            for lname, label in LABELS.items():
                for hidden in (False, True):
                    for cname, c in (("first", None), ("deep", mid)):
                        sql = keys_sql(n is not None, label=label is not None, hidden=hidden, cursor=c is not None)
                        ts = []
                        for _ in range(3):
                            # One transaction per run, settings LOCAL to it, as _OrderedWalk does.
                            for name, value in _ORDERED_WALK_SETTINGS.items():
                                cur.execute(f"SET LOCAL {name} = {value}")
                            cur.execute(f"SET LOCAL plan_cache_mode = {mode}")
                            cur.execute("PREPARE p(text, text, timestamptz, uuid, int) AS " + sql)
                            cur.execute(
                                "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) EXECUTE p(%s, %s, %s, %s, %s)",
                                (uid, f"%{label}%" if label else None, c[0] if c else None,
                                 str(c[1]) if c else None, LIMIT + 1),
                            )
                            plan = cur.fetchone()[0][0]
                            ts.append(plan["Execution Time"])
                            cur.execute("DEALLOCATE p")
                            conn.rollback()
                        all_nodes = nodes(plan["Plan"], [])
                        env = [x for x in all_nodes if x.get("Relation Name") == "environments"]
                        read = sum((x["Actual Rows"] + x.get("Rows Removed by Filter", 0)) * x["Actual Loops"]
                                   for x in env)
                        scans = ",".join(
                            f'{x.get("Index Name", x["Node Type"])}'
                            f'[{"cond" if "Index Cond" in x else "-"}{"+filter" if "Filter" in x else ""}]'
                            for x in env
                        )
                        shape = sorted({x["Node Type"] for x in all_nodes
                                        if x["Node Type"] in ("Merge Append", "Append", "Group", "HashAggregate",
                                                              "Sort", "Incremental Sort", "Seq Scan",
                                                              "Bitmap Heap Scan")})
                        print(f"{mode[6:12]:6} {who:10} {lname:6} {'hidden' if hidden else 'shown':6} {cname:5} "
                              f"ms={statistics.median(ts):8.2f} "
                              f"buf={plan['Plan'].get('Shared Hit Blocks', 0) + plan['Plan'].get('Shared Read Blocks', 0):6} "
                              f"envread={read:7} hashed={'hashed SubPlan' in str(plan)} jit={'JIT' in plan} "
                              f"shape={'/'.join(shape)} scans={scans}", flush=True)


if __name__ == "__main__":
    main()
