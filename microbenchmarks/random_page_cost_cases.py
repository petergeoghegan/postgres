#!/usr/bin/env python3
"""Plan choices from published random_page_cost complaints, under
random_page_cost 4 and 1.1 and effective_io_concurrency 16 and 1.

Cases (each has a setup file random_page_cost_cases_<case>.sql, usable by hand
with psql -f; -v nrows=N changes the size):

  lateral        the shayon.dev post: LATERAL ... ORDER BY id LIMIT 500 over a
                 queue table whose statistics predate the rows' completion;
                 the stock planner walks the primary key and filters.
  orderby_limit  pgsql-performance 2011 "Query with order by and limit is
                 very slow - wrong index used": WHERE source_id = ... ORDER BY
                 id LIMIT 200 with the matching rows at the end of the table;
                 the stock planner walks the primary key and filters instead
                 of using the (source_id, id) index.
  bitmapand      pgsql-hackers 2017 "Bitmap scan is undercosted?": two
                 non-independent single-column indexes; the BitmapAnd plan is
                 costed below the plain index scan and runs slower cached.

For each setting the script prints the EXPLAIN (ANALYZE, BUFFERS) output of
the plan the planner picks (best of --runs), then a summary with the costs of
the plan alternatives (another index hidden with indisvalid = false inside a
rolled-back transaction, or enable_* switched) and the execution time.
Everything is cached after the first run.

  random_page_cost_cases.py [--case lateral|orderby_limit|bitmapand|all]
                            [--conninfo "host=... port=... dbname=..." | --master]
                            [--nrows N] [--rebuild] [--runs N] [--no-plans]
"""
import argparse
import os
import re

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = [(4.0, 16), (4.0, 1), (1.1, 16), (1.1, 1)]

CASES = {
    "lateral": dict(
        sql="random_page_cost_cases_lateral.sql",
        query="""SELECT t1.*, t2.*
FROM table_1 t1
  CROSS JOIN LATERAL (SELECT * FROM table_2
                      WHERE table_2.table_1_id = t1.id
                        AND table_2.completed_at IS NULL
                      ORDER BY table_2.id LIMIT 500) t2
WHERE t1.id = ANY('{150,250}')""",
        built="""SELECT count(*) FROM pg_stats
                 WHERE tablename = 'table_2' AND attname = 'completed_at' AND null_frac = 1""",
        facts="""SELECT 'table_1_id ' || table_1_id || ': ' || count(*) FILTER (WHERE completed_at IS NULL)
                        || ' incomplete of ' || count(*) || ' rows'
                 FROM table_2 WHERE table_1_id IN (150, 250) GROUP BY table_1_id ORDER BY table_1_id""",
        url="https://www.shayon.dev/post/2024/55/100x-faster-query-in-aurora-postgres-with-a-lower-random_page_cost/",
        # alternatives: (label, hide this index, or set these GUCs)
        alternatives=[("pkey walk", dict(hide="idx_complete_table_2_on_table_1_id_id_where_completed_at_is_null")),
                      ("composite index", dict(hide="table_2_pkey"))],
        labels=[("composite index", "idx_complete_table_2_on_table_1_id"), ("pkey walk", "Index Scan using table_2_pkey")],
        scan_line="Index Scan using",
        note="the post: stock planner at random_page_cost 4 walked table_2_pkey, 146,339 rows removed per loop,"
             " 11.3 s on its cloud storage; at 1.1 the composite index, 1.5 ms",
    ),
    "orderby_limit": dict(
        sql="random_page_cost_cases_orderby_limit.sql",
        query="SELECT * FROM records WHERE source_id = 'wbc' ORDER BY id LIMIT 200",
        built="SELECT count(*) FROM pg_class WHERE relname = 'source_id_id_idx'",
        facts="""SELECT 'source ' || source_id || ': ' || count(*) || ' rows, ids ' || min(id) || '..' || max(id)
                 FROM records WHERE source_id IN ('wbc', 'ebuw') GROUP BY source_id ORDER BY source_id""",
        url="https://www.postgresql.org/message-id/flat/15615.1317654751%40sss.pgh.pa.us",
        alternatives=[("pkey walk", dict(hide="source_id_id_idx")),
                      ("(source_id, id) index", dict(hide="records_pkey"))],
        labels=[("(source_id, id) index", "Index Scan using source_id_id_idx"), ("pkey walk", "Index Scan using records_pkey"),
                ("source_id index + sort", "source_id_idx")],
        scan_line="Index Scan using",
        note="the thread: stock planner walked records_pkey (124 s), random_page_cost 2.0 and 1.5 did not help;"
             " the (source_id, id) index returned the rows in 0.7 s",
    ),
    "bitmapand": dict(
        sql="random_page_cost_cases_bitmapand.sql",
        query="SELECT count(*) FROM aaa WHERE num = 1 AND flag = true",
        built="SELECT count(*) FROM pg_class WHERE relname = 'i2'",
        facts="SELECT 'num = 1 rows: ' || count(*) || ', all with flag = true: ' || bool_and(flag) FROM aaa WHERE num = 1",
        url="https://www.postgrespro.com/list/thread-id/2358338",
        alternatives=[("bitmap (BitmapAnd)", dict(gucs={"enable_indexscan": "off"})),
                      ("index scan on i1", dict(gucs={"enable_bitmapscan": "off"}))],
        labels=[("bitmap (BitmapAnd)", "BitmapAnd"), ("bitmap on i1", "Bitmap Index Scan on i1"),
                ("index scan on i1", "Index Scan using i1"), ("seq scan", "Seq Scan on aaa")],
        scan_line=None,
        note="the thread (random_page_cost 1.5): BitmapAnd plan cost 24822, 185 ms; index scan cost 67939, 68 ms, cached",
    ),
}


def build(conn, case, nrows):
    sql = open(os.path.join(HERE, CASES[case]["sql"])).read()
    if nrows:
        sql = sql.replace(":nrows", str(nrows))
    else:
        default = re.search(r"\\set nrows (\d+)", sql).group(1)
        sql = sql.replace(":nrows", default)
    with conn.cursor() as cur:
        for stmt in sql.split(";\n"):
            # drop comment lines and the psql \if/\set lines (psql -f handles those itself)
            stmt = "\n".join(l for l in stmt.splitlines()
                             if not l.strip().startswith("--") and not l.strip().startswith("\\"))
            if stmt.strip():
                cur.execute(stmt)


def explain(conn, query, analyze=True, hide=None, gucs=None):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        if hide:
            cur.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass", (hide,))
        for k, v in (gucs or {}).items():
            cur.execute(f"SET LOCAL {k} = '{v}'")
        cur.execute(("EXPLAIN (ANALYZE, BUFFERS, COSTS ON, TIMING OFF) " if analyze else "EXPLAIN ") + query)
        lines = [r[0] for r in cur.fetchall()]
        cur.execute("ROLLBACK")
    return lines


def summarize(case, lines):
    """(plan label, top node total cost, rows removed by the table scan's filter per loop, ms)"""
    c = CASES[case]
    label = next((lab for lab, pat in c["labels"] if any(pat in l for l in lines)), "?")
    total = float(re.search(r"\.\.([0-9.]+) rows", lines[0]).group(1))
    removed = 0
    if c["scan_line"]:
        i = next((n for n, l in enumerate(lines) if c["scan_line"] in l), None)
        if i is not None:
            for l in lines[i + 1:i + 6]:
                m = re.search(r"Rows Removed by Filter: (\d+)", l)
                if m:
                    removed = int(m.group(1))
                    break
    ms = next((float(re.search(r"Execution Time: ([0-9.]+)", l).group(1)) for l in lines if "Execution Time" in l), None)
    return label, total, removed, ms


def run_case(conn, case, args):
    c = CASES[case]
    print(f"\n######## {case}: {c['query'].splitlines()[0]} ...")
    print(f"         after {c['url']}")
    with conn.cursor() as cur:
        cur.execute(c["built"])
        have = cur.fetchone()[0] == 1
    if args.rebuild or not have:
        print(f"building ({args.nrows or 'default'} rows) ...", flush=True)
        build(conn, case, args.nrows)
    with conn.cursor() as cur:
        cur.execute(c["facts"])
        for r in cur.fetchall():
            print("  " + r[0])
    rows = []
    for rpc, eic in SETTINGS:
        with conn.cursor() as cur:
            cur.execute(f"SET random_page_cost = {rpc}")
            cur.execute(f"SET effective_io_concurrency = {eic}")
            cur.execute("SET max_parallel_workers_per_gather = 0")
        # each alternative forced: its cost, and its best time of args.runs
        alts = {}
        for lab, how in c["alternatives"]:
            cost = summarize(case, explain(conn, c["query"], analyze=False, **how))[1]
            best_ms = min(summarize(case, explain(conn, c["query"], **how))[3] for _ in range(args.runs))
            alts[lab] = (cost, best_ms)
        best = None
        for _ in range(args.runs):
            lines = explain(conn, c["query"])
            label, total, removed, ms = summarize(case, lines)
            if best is None or ms < best[3]:
                best = (label, total, removed, ms, lines)
        label, total, removed, ms, lines = best
        if not args.no_plans:
            print(f"\n== {case}: random_page_cost = {rpc}, effective_io_concurrency = {eic}: {label}"
                  f" (best of {args.runs}; alternatives forced: "
                  + ", ".join(f"{lab} cost {cost:.2f}, {ms:.2f} ms" for lab, (cost, ms) in alts.items()) + ")")
            print("\n".join("   " + l for l in lines))
        rows.append((rpc, eic, label, total, alts, removed, ms))
    altnames = [lab for lab, _ in c["alternatives"]]
    print(f"\n{'random_page_cost':>16} {'eic':>4} {'plan chosen':>24} {'cost':>10} {'ms':>9} {'removed':>9}  "
          + "  ".join(f"{a + ' cost/ms':>30}" for a in altnames))
    for rpc, eic, label, total, alts, removed, ms in rows:
        print(f"{rpc:16.1f} {eic:4d} {label:>24} {total:10.2f} {ms:9.2f} {removed:9d}  "
              + "  ".join(f"{alts[a][0]:>18.2f} / {alts[a][1]:8.2f}" for a in altnames))
    print(f"({c['note']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="all", choices=["all"] + list(CASES))
    ap.add_argument("--conninfo", default=None,
                    help="libpq connection string; default: the patch cluster as prefetch_benchmark.py "
                         "knows it (host=/tmp port=5432 dbname=regression user=pg)")
    ap.add_argument("--master", action="store_true", help="use the master cluster instead (port 5555)")
    ap.add_argument("--nrows", type=int, default=None, help="rows for the case's table (default: the case's own)")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--runs", type=int, default=3, help="repetitions per setting, best time reported")
    ap.add_argument("--no-plans", action="store_true", help="print only the summary tables")
    args = ap.parse_args()
    conninfo = args.conninfo or ("host=/tmp port=5555 dbname=regression user=pg" if args.master
                                 else "host=/tmp port=5432 dbname=regression user=pg")
    conn = psycopg.connect(conninfo, autocommit=True)
    for case in (list(CASES) if args.case == "all" else [args.case]):
        run_case(conn, case, args)


if __name__ == "__main__":
    main()
