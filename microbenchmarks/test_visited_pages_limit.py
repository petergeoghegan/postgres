#!/usr/bin/env python3
"""
Smoke test for the planner's VISITED_PAGES_LIMIT (selfuncs.c).

get_actual_variable_endpoint() runs an index-only scan to replace a histogram
endpoint with the column's true minimum or maximum.  Dead index entries at
that extreme each force a heap fetch; after VISITED_PAGES_LIMIT distinct heap
pages without a visible tuple the scan gives up and the histogram bound
stands.  Every dead entry visited is killed (LP_DEAD) on the way, so a
planning cycle that gives up still makes progress, and after about
dead_pages / VISITED_PAGES_LIMIT cycles the endpoint is found.

The test loads a small table whose first (or last) N heap pages are entirely
dead, then plans one query at a time in one session -- no concurrency -- and
records per planning cycle:

  heap     heap blocks touched by planning   (pg_statio_user_tables delta;
           the visibility map page the scan reads first counts as one)
  miss     how many of those missed shared buffers
  index    index blocks touched by planning  (pg_statio_user_tables delta)
  plan ms  planning time                     (EXPLAIN SUMMARY)
  found    endpoint found?  The row estimate collapses to about 1 as soon as
           the actual endpoint replaces the histogram bound (see probe_query).

A cycle that gives up reads VISITED_PAGES_LIMIT + 1 heap pages but only
kills the entries of the first VISITED_PAGES_LIMIT of them: it stops on the
first entry of the last page.  So each such cycle advances by exactly
VISITED_PAGES_LIMIT pages.

Checks per scenario:
  * no cycle touched more than VISITED_PAGES_LIMIT + 2 blocks (limit + 1
    heap pages, one visibility map page);
  * the endpoint is found on cycle ceil(dead_pages / limit), one cycle of
    slack (this proves that the give-up path still kills the entries it
    visited; with dead_pages <= limit that is the first cycle).

Building with VISITED_PAGES_LIMIT 0 (limit disabled) makes the first cycle
touch all dead_pages heap blocks, which fails the first check.

--evict drops the table's heap from shared buffers before every cycle, so
every heap fetch made by planning misses shared buffers.  The OS page cache
still serves the reads, so this stays fast.  The planner scans with a
non-MVCC snapshot, and heapam only prefetches during MVCC scans, so on this
branch the read stream stays out of the picture either way; the option is
for branches that lift that restriction.
"""

import argparse
import math
import os
import statistics
import subprocess
import sys
import time

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prefetch_benchmark import (
    MASTER_BIN, PATCH_BIN, MASTER_DATA_DIR, PATCH_DATA_DIR,
    MASTER_CONN, PATCH_CONN
)

TABLE = "vpl_test"
VISITED_PAGES_LIMIT = 100       # must match selfuncs.c
ROWS = 1_000_000                # ~4,400 heap pages, ~35 MB, loads in ~1 s
DEAD_PAGES = [50, 1000]         # one below the limit, one well above
SETTLED_CYCLES = 3              # extra cycles after the endpoint is found


def start_server(bin_dir, data_dir, conn, name):
    pg_ctl = os.path.join(bin_dir, "pg_ctl")
    subprocess.run([pg_ctl, "stop", "-D", data_dir, "-m", "fast"],
                   capture_output=True, check=False)
    result = subprocess.run(
        [pg_ctl, "start", "-w", "-D", data_dir,
         "-l", f"/tmp/{name}_visited_pages_test.log",
         "-o", f"-p {conn['port']} -c autovacuum=off"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n")
        sys.exit(f"could not start {name} server")


def stop_server(bin_dir, data_dir):
    subprocess.run([os.path.join(bin_dir, "pg_ctl"), "stop", "-D", data_dir,
                    "-m", "fast"], capture_output=True, check=False)


def load_table(cur, rows):
    """Load ROWS sequential bigints; returns the heap size in pages."""
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cur.execute(f"CREATE UNLOGGED TABLE {TABLE} (id bigint PRIMARY KEY) "
                "WITH (autovacuum_enabled = off)")
    cur.execute(f"INSERT INTO {TABLE} SELECT i FROM generate_series(1, %s) i",
                (rows,))
    cur.execute(f"VACUUM ANALYZE {TABLE}")
    cur.execute(f"SELECT pg_relation_size(%s) / current_setting('block_size')::int",
                (TABLE,))
    return cur.fetchone()[0]


def delete_pages(cur, direction, dead_pages, heap_pages):
    """Delete every row on the first (min) or last (max) dead_pages pages."""
    if direction == "min":
        cur.execute(f"DELETE FROM {TABLE} WHERE ctid < %s::tid",
                    (f"({dead_pages},1)",))
    else:
        cur.execute(f"DELETE FROM {TABLE} WHERE ctid >= %s::tid",
                    (f"({heap_pages - dead_pages},1)",))
    return cur.rowcount


def probe_query(cur, direction, dead_pages, heap_pages):
    """
    A query that makes the planner look up the endpoint and whose estimate
    collapses once the lookup succeeds.

    The constant must lie inside the first (last) histogram bin, or the
    planner never consults the endpoint.  Within that bin it is the endpoint
    that survives the delete: a successful lookup then finds no histogram
    entry below (above) the constant and estimates about 1 row; a failed one
    interpolates the stale bin and estimates about the number of deleted
    rows, or a whole bin when the dead run is longer than that.  The endpoint
    is read from the endpoint page alone, so no dead page and no index entry
    is touched.
    """
    cur.execute("SELECT histogram_bounds::text::bigint[] FROM pg_stats "
                "WHERE tablename = %s AND attname = 'id'", (TABLE,))
    bounds = cur.fetchone()[0]
    if direction == "min":
        page = dead_pages
    else:
        page = heap_pages - dead_pages - 1
    cur.execute(f"SELECT min(id), max(id) FROM {TABLE} "
                "WHERE ctid >= %s::tid AND ctid < %s::tid",
                (f"({page},1)", f"({page + 1},1)"))
    lo, hi = cur.fetchone()
    if direction == "min":
        return f"SELECT * FROM {TABLE} WHERE id < {min(lo, bounds[1])}"
    return f"SELECT * FROM {TABLE} WHERE id > {max(hi, bounds[-2])}"


def plan(cur, query):
    """Returns (planning time in ms, estimated rows)."""
    cur.execute(f"EXPLAIN (FORMAT JSON, SUMMARY ON) {query}")
    out = cur.fetchone()[0][0]
    return out["Planning Time"], out["Plan"]["Plan Rows"]


def blocks(cur):
    """
    (heap blocks, heap misses, index blocks) touched so far, with all pending
    stats flushed first.
    """
    cur.execute("SELECT pg_stat_force_next_flush()")
    cur.execute("SELECT heap_blks_read + heap_blks_hit, heap_blks_read, "
                "       idx_blks_read + idx_blks_hit "
                "FROM pg_statio_user_tables WHERE relname = %s", (TABLE,))
    return cur.fetchone()


def evict_heap(cur):
    cur.execute("SELECT pg_buffercache_evict_relation(%s)", (TABLE,))


def run_scenario(conn, direction, dead_pages, args):
    """Load, delete, plan until the endpoint is found; returns the cycles."""
    cur = conn.cursor()
    cur.execute("SET stats_fetch_consistency = none")
    if args.evict:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
    heap_pages = load_table(cur, args.rows)
    query = probe_query(cur, direction, dead_pages, heap_pages)
    _, stale_rows = plan(cur, query)    # endpoint == histogram bound here
    dead_rows = delete_pages(cur, direction, dead_pages, heap_pages)

    expected = max(1, math.ceil(dead_pages / VISITED_PAGES_LIMIT))
    cycles = []
    found_at = None
    for cycle in range(1, expected + 1 + args.settled_cycles + 5):
        if args.evict:
            evict_heap(cur)
        heap0, miss0, idx0 = blocks(cur)
        plan_ms, rows = plan(cur, query)
        heap1, miss1, idx1 = blocks(cur)
        found = rows < stale_rows / 2
        cycles.append((cycle, heap1 - heap0, miss1 - miss0, idx1 - idx0,
                       plan_ms, rows, found))
        if found and found_at is None:
            found_at = cycle
        if found_at and cycle >= found_at + args.settled_cycles:
            break
    if not args.keep_table:
        cur.execute(f"DROP TABLE {TABLE}")
    cur.close()

    print(f"  {direction} endpoint, {dead_pages} dead pages "
          f"({dead_rows:,} rows) of {heap_pages:,}; "
          f"stale estimate {stale_rows:,} rows; query: {query.split('WHERE')[1].strip()}")
    print(f"  {'cycle':>5} {'heap':>6} {'miss':>6} {'index':>6} {'plan ms':>8} "
          f"{'est rows':>9}  found")
    for cycle, heap, miss, idx, plan_ms, rows, found in cycles:
        print(f"  {cycle:>5} {heap:>6} {miss:>6} {idx:>6} {plan_ms:>8.3f} "
              f"{rows:>9,}  {'yes' if found else 'no'}")

    problems = []
    for cycle, heap, miss, idx, plan_ms, rows, found in cycles:
        if heap > VISITED_PAGES_LIMIT + 2:
            problems.append(f"cycle {cycle} touched {heap} heap blocks "
                            f"(limit {VISITED_PAGES_LIMIT})")
    if found_at is None:
        problems.append(f"endpoint never found in {len(cycles)} cycles")
    elif not expected <= found_at <= expected + 1:
        problems.append(f"endpoint found on cycle {found_at}, "
                        f"expected {expected}-{expected + 1}")
    if problems:
        print("  FAIL: " + "; ".join(problems))
    else:
        print(f"  PASS: found on cycle {found_at} (expected {expected}-{expected + 1})")

    giving_up = [c[4] for c in cycles if not c[6]]
    settled = [c[4] for c in cycles if c[6]][1:]   # skip the finding cycle
    return {
        "cycles": found_at,
        "max_heap": max(c[1] for c in cycles),
        "total_heap": sum(c[1] for c in cycles),
        "total_index": sum(c[3] for c in cycles),
        "giveup_ms": statistics.mean(giving_up) if giving_up else float("nan"),
        "settled_ms": statistics.mean(settled) if settled else float("nan"),
        "pass": not problems,
    }


def run_server(name, conn_details, args):
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")
    results = {}
    with psycopg.connect(**conn_details, autocommit=True) as conn:
        for dead_pages in args.dead_pages:
            for direction in args.directions:
                print()
                results[(direction, dead_pages)] = \
                    run_scenario(conn, direction, dead_pages, args)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Smoke test for the planner's VISITED_PAGES_LIMIT",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--master-only", action="store_true")
    parser.add_argument("--patch-only", action="store_true")
    parser.add_argument("--rows", type=int, default=ROWS,
                        help="rows loaded per scenario")
    parser.add_argument("--dead-pages", type=int, nargs="+", default=DEAD_PAGES,
                        help="heap pages deleted at the endpoint, one scenario each")
    parser.add_argument("--direction", choices=["min", "max", "both"],
                        default="both", help="which endpoint(s) to test")
    parser.add_argument("--settled-cycles", type=int, default=SETTLED_CYCLES,
                        help="planning cycles recorded after the endpoint is found")
    parser.add_argument("--evict", action="store_true",
                        help="evict the heap from shared buffers before each cycle")
    parser.add_argument("--keep-table", action="store_true",
                        help=f"leave the last {TABLE} in place")
    args = parser.parse_args()
    args.directions = ["min", "max"] if args.direction == "both" else [args.direction]

    servers = []
    if not args.patch_only:
        servers.append(("master", MASTER_BIN, MASTER_DATA_DIR, MASTER_CONN))
    if not args.master_only:
        servers.append(("patch", PATCH_BIN, PATCH_DATA_DIR, PATCH_CONN))
    if not servers:
        sys.exit("--master-only and --patch-only are mutually exclusive")

    print(f"VISITED_PAGES_LIMIT smoke test: {args.rows:,} rows, dead pages "
          f"{args.dead_pages}, endpoints {args.directions}"
          f"{', heap evicted before each cycle' if args.evict else ''}")

    all_results = {}
    started = time.perf_counter()
    for name, bin_dir, data_dir, conn in servers:
        start_server(bin_dir, data_dir, conn, name)
        try:
            all_results[name] = run_server(name, conn, args)
        finally:
            stop_server(bin_dir, data_dir)

    print(f"\n{'=' * 72}\nSUMMARY [{time.perf_counter() - started:.1f}s]\n{'=' * 72}")
    print(f"{'scenario':<16} {'server':<7} {'result':<6} {'cycles':>6} "
          f"{'max heap':>8} {'tot heap':>8} {'tot idx':>8} "
          f"{'give-up ms':>10} {'settled ms':>10}")
    for key in next(iter(all_results.values())):
        for name, results in all_results.items():
            r = results[key]
            print(f"{key[0] + ' ' + str(key[1]) + ' pages':<16} {name:<7} "
                  f"{'PASS' if r['pass'] else 'FAIL':<6} "
                  f"{r['cycles'] if r['cycles'] else '-':>6} "
                  f"{r['max_heap']:>8} {r['total_heap']:>8} {r['total_index']:>8} "
                  f"{r['giveup_ms']:>10.3f} {r['settled_ms']:>10.3f}")

    if not all(r["pass"] for results in all_results.values()
               for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
