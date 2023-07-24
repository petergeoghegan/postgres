#!/usr/bin/env python3
"""Plan-choice sweep over selectivity on a small table, after Franck Pachot's
"PostgreSQL with modern storage: what about a lower random_page_cost?"
(https://dev.to/aws-heroes/postgresql-with-modern-storage-what-about-a-lower-randompagecost-5b7f):
demo(id bigserial primary key, val int) with val random, so id is correlated
with the heap order and val is not.  For X from 1% to 100% of the key range,
the planner's choice for  SELECT * FROM demo WHERE val < X  (and  id < X)  is
recorded under each setting, and the switch points between the plan families
(bitmap heap scan, plain index scan, seq scan) are printed like the post's
table.  A guard: with the default random_page_cost the post's planner went
bitmap -> seq at 38%; at 1.1 it used a plain index scan from 17% to 56%,
reading 30x the buffers of the seq scan.  --time measures the three forced
plans at a few selectivities, cached, so the chosen family can be judged.

  random_page_cost_sweep.py [--conninfo ... | --master] [--nrows 100000] [--rebuild] [--time]
"""
import argparse
import re

import psycopg

SETTINGS = [("rpc 4, eic 16", 4.0, 16), ("rpc 4, eic 1", 4.0, 1), ("rpc 1.1, eic 16", 1.1, 16), ("rpc 1.1, eic 1", 1.1, 1)]
FAMILIES = [("bitmap", "Bitmap Heap Scan"), ("index", "Index Scan"), ("index only", "Index Only Scan"), ("seq", "Seq Scan")]


def family(lines):
    return next((f for f, pat in FAMILIES if any(pat in l for l in lines)), "?")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conninfo", default=None)
    ap.add_argument("--master", action="store_true")
    ap.add_argument("--nrows", type=int, default=100_000)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--time", action="store_true", help="time the forced plans at 5, 10, 20, 40 and 60%% of the range")
    args = ap.parse_args()
    conninfo = args.conninfo or ("host=/tmp port=5555 dbname=regression user=pg" if args.master
                                 else "host=/tmp port=5432 dbname=regression user=pg")
    conn = psycopg.connect(conninfo, autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'demo'")
    r = cur.fetchone()
    if args.rebuild or r is None or abs(r[0] - args.nrows) > args.nrows * 0.01:
        print(f"building demo ({args.nrows} rows) ...", flush=True)
        cur.execute("DROP TABLE IF EXISTS demo")
        cur.execute("CREATE TABLE demo (id bigserial PRIMARY KEY, val int)")
        cur.execute(f"INSERT INTO demo (val) SELECT random() * {args.nrows} FROM generate_series(1, {args.nrows})")
        cur.execute("CREATE INDEX demo_val ON demo (val)")
        cur.execute("VACUUM ANALYZE demo")
    cur.execute("SELECT relpages, reltuples::bigint FROM pg_class WHERE relname = 'demo'")
    pages, rows = cur.fetchone()
    cur.execute("SELECT attname, round(correlation::numeric, 3) FROM pg_stats WHERE tablename = 'demo' ORDER BY 1")
    print("after https://dev.to/aws-heroes/postgresql-with-modern-storage-what-about-a-lower-randompagecost-5b7f")
    print(f"demo: {rows} rows, {pages} pages; correlation " + ", ".join(f"{a} {c}" for a, c in cur.fetchall()))
    cur.execute("SET max_parallel_workers_per_gather = 0")
    for col in ("val", "id"):
        print(f"\n-- SELECT * FROM demo WHERE {col} < X, X from 1% to 100% of the range: plan family per setting")
        print(f"{'setting':>16} " + "".join(f"{f:>18}" for f, _ in FAMILIES))
        for name, rpc, eic in SETTINGS:
            cur.execute(f"SET random_page_cost = {rpc}")
            cur.execute(f"SET effective_io_concurrency = {eic}")
            seen = {}
            for pct in range(1, 101):
                x = int(args.nrows * pct / 100)
                cur.execute(f"EXPLAIN SELECT * FROM demo WHERE {col} < {x}")
                f = family([r[0] for r in cur.fetchall()])
                lo, hi = seen.get(f, (pct, pct))
                seen[f] = (min(lo, pct), max(hi, pct))
            print(f"{name:>16} " + "".join(f"{(f'{seen[f][0]}-{seen[f][1]}%' if f in seen else '-'):>18}" for f, _ in FAMILIES))
    if args.time:
        print("\n-- forced plans, cached (best of 3 ms): val < X")
        print(f"{'X':>5} {'bitmap':>10} {'index':>10} {'seq':>10}")
        for pct in (5, 10, 20, 40, 60):
            x = int(args.nrows * pct / 100)
            times = {}
            for f, gucs in (("bitmap", "SET enable_seqscan = off; SET enable_indexscan = off; SET enable_bitmapscan = on"),
                            ("index", "SET enable_seqscan = off; SET enable_bitmapscan = off; SET enable_indexscan = on"),
                            ("seq", "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_seqscan = on")):
                best = None
                for _ in range(3):
                    cur.execute("BEGIN")
                    for g in gucs.split("; "):
                        cur.execute("SET LOCAL" + g[3:])
                    cur.execute(f"EXPLAIN (ANALYZE, TIMING OFF) SELECT * FROM demo WHERE val < {x}")
                    ms = next(float(re.search(r"Execution Time: ([0-9.]+)", r[0]).group(1)) for r in cur.fetchall() if "Execution Time" in r[0])
                    cur.execute("ROLLBACK")
                    best = ms if best is None else min(best, ms)
                times[f] = best
            print(f"{pct:4d}% {times['bitmap']:10.3f} {times['index']:10.3f} {times['seq']:10.3f}")


if __name__ == "__main__":
    main()
