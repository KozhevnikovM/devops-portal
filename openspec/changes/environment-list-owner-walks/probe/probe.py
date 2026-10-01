"""EXPLAIN (ANALYZE, BUFFERS) matrix for the environments page (#496).

Runs the app's own `_page_stmt` (custom plan via psycopg2 literals, and generic plan via
PREPARE + plan_cache_mode=force_generic_plan) and the `_children_batch` statement for the
selected page, and prints one row per case.
"""
import json
import statistics
import sys
from uuid import UUID

from sqlalchemy import String, cast, create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg

from app.domain.pagination import KeysetCursor
from app.infrastructure.database.models import BookingModel, NamespaceModel, StaticVMModel, UserModel
from app.infrastructure.repositories.environment_repo import _page_stmt

URL = "postgresql+psycopg2://portal:portal@localhost:5433/env_probe_496"
LIMIT = 50
USERS = {
    "all": None,
    "heavy": "00000000-0000-0000-0000-000000000001",
    "dispatcher": "00000000-0000-0000-0000-000000000002",
    "rare": "00000000-0000-0000-0000-000000000003",
    "viewer": "00000000-0000-0000-0000-000000000004",
}
LABELS = {"none": None, "sparse": "needle", "dense": "web"}
REPS = 3


def walk(node, out):
    out.append(node)
    for c in node.get("Plans", []):
        walk(c, out)
    return out


def summarize(plan_json):
    top = plan_json[0]
    nodes = walk(top["Plan"], [])
    env = [n for n in nodes if n.get("Relation Name") == "environments"]
    bk = [n for n in nodes if n.get("Relation Name") == "bookings"]
    sorts = [n for n in nodes if n["Node Type"] == "Sort"]
    env_read = sum((n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"] for n in env)
    env_desc = "; ".join(
        f'{n["Node Type"]}{"(" + n["Index Name"] + ")" if "Index Name" in n else ""}'
        f'{" cond=" + n["Index Cond"] if "Index Cond" in n else ""}'
        f'{" filter=" + n["Filter"][:90] if "Filter" in n else ""}'
        for n in env
    )
    bk_probes = sum(n["Actual Loops"] for n in bk)
    bk_desc = ",".join(sorted({f'{n["Node Type"]}:{n.get("Index Name", "-")}' for n in bk}))
    sort_in = max((n["Plans"][0]["Actual Rows"] * n["Plans"][0]["Actual Loops"] for n in sorts), default=0)
    buffers = top["Plan"].get("Shared Hit Blocks", 0) + top["Plan"].get("Shared Read Blocks", 0)
    return {
        "ms": top["Execution Time"], "plan_ms": top["Planning Time"], "buf": buffers,
        "env_read": env_read, "env_plan": env_desc, "bk_probes": bk_probes, "bk_plan": bk_desc,
        "sort_in": sort_in, "jit_ms": top.get("JIT", {}).get("Functions", 0),
    }


def explain_custom(conn, stmt):
    c = stmt.compile(dialect=postgresql.psycopg2.dialect())
    out = []
    for _ in range(REPS):
        raw = conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + str(c), c.params).scalar()
        out.append(raw)
    rows = conn.exec_driver_sql(str(c), c.params).all()
    return out, rows


def explain_generic(conn, stmt, mode="force_generic_plan"):
    c = stmt.compile(dialect=pg_asyncpg.dialect())
    params = [c.params[k] for k in c.positiontup]
    conn.exec_driver_sql("DEALLOCATE ALL")
    conn.exec_driver_sql(f"SET plan_cache_mode = {mode}")
    conn.exec_driver_sql("PREPARE p AS " + str(c))
    lit = ", ".join(conn.exec_driver_sql("SELECT quote_nullable(%(v)s::text)", {"v": str(p) if p is not None else None}).scalar() for p in params)
    cur = conn.connection.dbapi_connection.cursor()
    if mode == "auto":
        for _ in range(6):
            cur.execute(f"EXECUTE p({lit})"); cur.fetchall()
    out = []
    for _ in range(REPS):
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) EXECUTE p({lit})")
        out.append(cur.fetchone()[0])
    conn.exec_driver_sql("DEALLOCATE p")
    conn.exec_driver_sql("RESET plan_cache_mode")
    return out


def children_stmt(env_ids):
    return (
        select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
        .join(UserModel, cast(UserModel.id, String) == BookingModel.user_id, isouter=True)
        .outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
        .outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
        .where(BookingModel.environment_id.in_(env_ids))
        .order_by(BookingModel.environment_id, BookingModel.created_at)
    )


def med(runs):
    s = [summarize(r) for r in runs]
    best = s[len(s) // 2]
    best["ms"] = statistics.median(x["ms"] for x in s)
    return best


def main():
    tag = sys.argv[1]
    eng = create_engine(URL, isolation_level="AUTOCOMMIT")
    results = []
    with eng.connect() as conn:
        n = conn.exec_driver_sql("SELECT count(*) FROM environments").scalar()
        for who, uid in USERS.items():
            for lname, label in LABELS.items():
                for released in ("shown", "hidden"):
                    inc = released == "shown"
                    # deep cursor: the middle of this user's visible history (unfiltered by label/released)
                    q = "SELECT created_at, id FROM environments"
                    if uid:
                        q += f" WHERE user_id = '{uid}' OR created_by = '{uid}'"
                    q += " ORDER BY created_at DESC, id DESC OFFSET (SELECT count(*)/2 FROM environments" + (f" WHERE user_id = '{uid}' OR created_by = '{uid}'" if uid else "") + ") LIMIT 1"
                    mid = conn.exec_driver_sql(q).one()
                    for cur_name, cur in (("first", None), ("deep", KeysetCursor(created_at=mid[0], id=mid[1]))):
                        stmt = _page_stmt(uid, label=label, include_released=inc, limit=LIMIT, after=cur)
                        runs, rows = explain_custom(conn, stmt)
                        custom = med(runs)
                        generic = med(explain_generic(conn, stmt))
                        auto = med(explain_generic(conn, stmt, "auto"))
                        ids = [r[0] for r in rows[:LIMIT]]
                        if ids:
                            craws = [conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + str(cc)).scalar()
                                     for cc in [children_stmt(ids).compile(dialect=postgresql.psycopg2.dialect(), compile_kwargs={"literal_binds": True})] * REPS]
                            child = med(craws)
                            child_rows = conn.exec_driver_sql("SELECT count(*) FROM bookings WHERE environment_id = ANY(%(ids)s::uuid[])", {"ids": [str(i) for i in ids]}).scalar()
                        else:
                            child = {"ms": 0, "buf": 0}
                            child_rows = 0
                        r = {"n": n, "who": who, "label": lname, "released": released, "cursor": cur_name,
                             "returned": min(len(rows), LIMIT), "more": len(rows) > LIMIT,
                             "custom": custom, "generic": generic, "auto": auto,
                             "child_ms": child["ms"], "child_buf": child["buf"], "child_rows": child_rows}
                        results.append(r)
                        print(f'{who:10} {lname:6} {released:6} {cur_name:5} ret={r["returned"]:2} '
                              f'| C {custom["ms"]:8.2f}ms buf={custom["buf"]:6} envread={custom["env_read"]:7} '
                              f'bkprobe={custom["bk_probes"]:6} sort_in={custom["sort_in"]:6} jitfn={custom["jit_ms"]} '
                              f'| G {generic["ms"]:8.2f}ms buf={generic["buf"]:6} envread={generic["env_read"]:7} jitfn={generic["jit_ms"]} '
                              f'| A {auto["ms"]:7.2f}ms envread={auto["env_read"]:7} '
                              f'| child {child["ms"]:.2f}ms buf={child["buf"]} rows={child_rows}', flush=True)
                        print(f'    C: {custom["env_plan"]} || {custom["bk_plan"]}')
                        if generic["env_plan"] != custom["env_plan"]:
                            print(f'    G: {generic["env_plan"]} || {generic["bk_plan"]}')
    with open(f"results-{tag}.json", "w") as f:
        json.dump(results, f, indent=1, default=str)


if __name__ == "__main__":
    main()
