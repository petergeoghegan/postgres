#!/usr/bin/env python3
"""
Standalone reproducer for the nested loop anti join prefetch regression
(Tomas Vondra's antijoin.sql, see the header of that file for the analysis).

Why this is not a prefetch_benchmark.py suite: the regression only shows with
a shared_buffers that fits the join's real working set but NOT that working
set plus the read-ahead the inner index scan abandons on every rescan.  The
prefetch_benchmark.py servers run with whatever shared_buffers the data
directory is configured with, and the harness evicts and prewarms per query;
this reproducer needs to size shared_buffers itself and start each measurement
from a cold buffer pool (a server restart), so it drives the server directly.

What it measures, per configuration (patch: prefetch on and off; master: no
GUC): restart the server (cold buffer pool), optionally drop the OS page
cache, then run the anti join N times under EXPLAIN (ANALYZE, BUFFERS, TIMING
OFF).  Per run it reports the server-side execution time and the blocks read
by the whole query and by the inner Index Scan node (shared read=...).  Those
counts are timing independent.  Note that the rescan itself (ExecReScan ->
heapam_index_scan_rescan -> read_stream_reset) runs outside the inner node's
instrumentation window, so the reads the reset issues are charged to the
Nested Loop node and only show up in the query total; the inner node's own
reads are the re-reads of the working set the abandoned read-ahead evicted.
On an unfixed build both stay high on EVERY run (steady state); on a fixed
build both drop to ~0 after the first, cold run.

Mechanism (index-prefetch-post-v34.0, 2026-09-05): read_stream_reset() sets
readahead_distance = -1 and drains the stream with read_stream_next_buffer(),
but leaves pending_read_nblocks alone.  Each drain iteration ends in
read_stream_look_ahead(), whose tail calls read_stream_should_issue_now(),
which returns true whenever readahead_distance <= 0 and a pending read exists.
So the pending read -- for blocks the stream is about to throw away -- is
started, and the next drain iteration hands the buffers straight to
ReleaseBuffer().  heapam_index_scan_rescan() resets the stream once per outer
row, so a nestloop anti join pays one wasted read of up to io_combine_limit
blocks per rescan.  Fix: zero pending_read_nblocks in read_stream_reset().

Usage:
    ./antijoin_regress.py                       # patch server, prefetch on + off
    ./antijoin_regress.py --server master       # master server (no prefetch GUC)
    ./antijoin_regress.py --shared-buffers 32MB # both working sets too big: waste
                                                # still visible in reads, not in time
    ./antijoin_regress.py --io_method io_uring  # sync never shows it: reads are
                                                # issued before the reset anyway
    ./antijoin_regress.py --drop-os-cache       # wasted reads hit the device
    ./antijoin_regress.py --rebuild             # drop and reload the test tables

Requirements: the server's data directory must already exist and hold a
'regression' database (same as prefetch_benchmark.py).  The test tables
(anti_inner, ~500MB, and anti_outer) are built on first use, which takes a
minute or two; they are left in place for later runs.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from statistics import median

import psycopg

from prefetch_benchmark import (
    BASELINE_CONFIGS,
    BUILD_HAS_PREFETCH,
    TESTBRANCH_CONFIGS,
    clear_os_cache,
)

SERVERS = {**BASELINE_CONFIGS, **TESTBRANCH_CONFIGS}

# Planner GUCs that force the plan of interest: a nested loop anti join with a
# plain (not index-only) index scan on the inner side, which is what
# "heapam: Add index scan I/O prefetching" targets.
PLAN_GUCS = {
    "enable_hashjoin": "off",
    "enable_mergejoin": "off",
    "enable_bitmapscan": "off",
    "enable_seqscan": "off",
    "enable_material": "off",
    "enable_memoize": "off",
    "max_parallel_workers_per_gather": "0",
}


def parse_arguments():
    p = argparse.ArgumentParser(
        description="Nested loop anti join / read_stream_reset() prefetch regression reproducer",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", choices=sorted(SERVERS), default="patch",
                   help="Which server (bin + data dir + port) to run against (default: patch)")
    p.add_argument("--shared-buffers", default="160MB",
                   help="shared_buffers for the server; must fit the join's working set "
                        "(~keys * hit_row blocks) but not twice that (default: 160MB)")
    p.add_argument("--io_method", default="worker",
                   help="io_method GUC (default: worker; sync cannot show the regression)")
    p.add_argument("--effective_io_concurrency", type=int, default=16,
                   help="effective_io_concurrency GUC (default: 16)")
    p.add_argument("--io_combine_limit", type=int, default=16,
                   help="io_combine_limit in 8kB units (default: 16 = 128kB)")
    p.add_argument("--pg-option", action="append", default=[], metavar="OPT",
                   help="Extra server command-line option, e.g. '-c io_max_workers=32' (repeatable)")
    p.add_argument("--runs", type=int, default=4,
                   help="Runs per configuration after the server restart; the first run "
                        "is the cold one (default: 4)")
    p.add_argument("--drop-os-cache", action="store_true",
                   help="Drop the OS page cache after each restart (wasted reads then hit the device)")
    p.add_argument("--prefetch", choices=["on", "off", "both"], default="both",
                   help="Which prefetch settings to run on a build that has the "
                        "debug_disable_indexscan_prefetch GUC (on = prefetching "
                        "enabled; default: both)")
    p.add_argument("--rebuild", action="store_true",
                   help="Drop and reload anti_inner/anti_outer even if they exist")
    p.add_argument("--keep-running", action="store_true",
                   help="Leave the server running at the end (default: stop it)")

    d = p.add_argument_group("data set (only used when the tables are (re)built)")
    d.add_argument("--keys", type=int, default=1000,
                   help="Distinct keys in anti_inner (default: 1000)")
    d.add_argument("--rows-per-key", type=int, default=64,
                   help="Rows (= heap blocks, via fillfactor=10 + 700 byte pad) per key (default: 64)")
    d.add_argument("--outer-rows", type=int, default=20000,
                   help="Rows in anti_outer, i.e. number of rescans (default: 20000)")
    p.add_argument("--hit-row", type=int, default=12,
                   help="Which row of a key group satisfies the non-indexable filter.  Far "
                        "enough in for the stream to have ramped up, far enough from the end "
                        "for a partial pending read to exist at reset time (default: 12)")
    return p.parse_args()


# ── server control ─────────────────────────────────────────────────────────
def pg_ctl(bin_dir, *cmd, check=True):
    return subprocess.run([os.path.join(bin_dir, "pg_ctl"), *cmd],
                          capture_output=True, text=True, check=check)


def stop_server(bin_dir, data_dir):
    if pg_ctl(bin_dir, "status", "-D", data_dir, check=False).returncode == 0:
        pg_ctl(bin_dir, "stop", "-D", data_dir, "-m", "fast")


def start_server(bin_dir, data_dir, conn_details, args, log_file):
    """(Re)start the server with this reproducer's settings and wait for it."""
    stop_server(bin_dir, data_dir)
    options = [
        "--autovacuum=off",
        "-c wal_level=minimal",
        "-c max_wal_senders=0",
        "-c max_connections=20",
        f"-c shared_buffers={args.shared_buffers}",
        f"-c io_method={args.io_method}",
        f"-c effective_io_concurrency={args.effective_io_concurrency}",
        f"-c io_combine_limit={args.io_combine_limit}",
        "-c io_max_combine_limit=128",
        f"-p {conn_details['port']}",
        *args.pg_option,
    ]
    cmd = [os.path.join(bin_dir, "pg_ctl"), "start", "-D", data_dir, "-l", log_file]
    for opt in options:
        cmd.extend(["-o", opt])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Error: failed to start server\n{result.stdout}\n{result.stderr}")
    for attempt in range(30):
        try:
            conn = psycopg.connect(**conn_details, connect_timeout=2)
            conn.close()
            return
        except (psycopg.OperationalError, psycopg.DatabaseError):
            time.sleep(0.5)
    sys.exit(f"Error: server did not accept connections; see {log_file}")


def connect(conn_details):
    conn = psycopg.connect(**conn_details)
    conn.autocommit = True
    return conn


# ── data set ───────────────────────────────────────────────────────────────
def ensure_data(conn, args):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('anti_inner') IS NOT NULL "
                    "AND to_regclass('anti_outer') IS NOT NULL")
        present = cur.fetchone()[0]
        if present and not args.rebuild:
            return
        print(f"Building test data (keys={args.keys}, rows_per_key={args.rows_per_key}, "
              f"outer_rows={args.outer_rows}), this takes a while...")
        t0 = time.perf_counter()
        cur.execute("DROP TABLE IF EXISTS anti_inner, anti_outer")
        # fillfactor = 10 plus the ~700 byte pad gives one row per heap block,
        # and inserting in key order makes each key group's blocks physically
        # contiguous.  Both are needed for the read stream to build a pending
        # read that spans several blocks.
        cur.execute("""
            CREATE TABLE anti_inner (
                k    int NOT NULL,
                sel  int NOT NULL,
                pad  char(700) NOT NULL
            ) WITH (fillfactor = 10)
        """)
        cur.execute("""
            INSERT INTO anti_inner
            SELECT k, r, ''
              FROM generate_series(1, %s) k,
                   generate_series(1, %s) r
             ORDER BY k, r
        """, (args.keys, args.rows_per_key))
        cur.execute("CREATE INDEX anti_inner_k_idx ON anti_inner (k)")
        cur.execute("CREATE TABLE anti_outer (k int NOT NULL)")
        cur.execute("INSERT INTO anti_outer SELECT 1 + (g %% %s) FROM generate_series(1, %s) g",
                    (args.keys, args.outer_rows))
        cur.execute("VACUUM (ANALYZE, FREEZE) anti_inner, anti_outer")
        cur.execute("CHECKPOINT")
        print(f"Data built in {time.perf_counter() - t0:.0f}s.")


def describe_data(conn, args):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pg_relation_size('anti_inner') / 8192,
                   pg_relation_size('anti_inner_k_idx') / 8192,
                   pg_relation_size('anti_outer') / 8192,
                   (SELECT count(DISTINCT k) FROM anti_inner),
                   (SELECT count(*) FROM anti_outer),
                   current_setting('shared_buffers'),
                   current_setting('io_method'),
                   current_setting('effective_io_concurrency'),
                   current_setting('io_combine_limit')
        """)
        (inner_blocks, idx_blocks, outer_blocks, keys, outer_rows,
         sb, iom, eic, icl) = cur.fetchone()
    useful = keys * args.hit_row
    print(f"anti_inner: {inner_blocks} heap blocks ({inner_blocks * 8 // 1024}MB), "
          f"{idx_blocks} index blocks; anti_outer: {outer_blocks} blocks, {outer_rows} rows")
    print(f"Working set the join needs: {keys} keys x hit_row {args.hit_row} = "
          f"{useful} blocks ({useful * 8 // 1024}MB); shared_buffers = {sb}")
    print(f"io_method = {iom}, effective_io_concurrency = {eic}, io_combine_limit = {icl}")


# ── measurement ────────────────────────────────────────────────────────────
def anti_query(args):
    return ("SELECT count(*) FROM anti_outer o WHERE NOT EXISTS "
            "(SELECT 1 FROM anti_inner i WHERE i.k = o.k AND i.sel = %d)" % args.hit_row)


def disable_prefetch_guc_value(prefetch):
    """Map a prefetch on/off setting to the value to use for the
    debug_disable_indexscan_prefetch GUC, whose meaning is inverted (on means
    that prefetching is disabled)."""
    return "off" if str(prefetch).lower() in ("on", "true", "1") else "on"


def set_gucs(conn, has_prefetch_guc, prefetch):
    with conn.cursor() as cur:
        for guc, value in PLAN_GUCS.items():
            cur.execute(f"SET {guc} = {value}")
        if has_prefetch_guc:
            cur.execute("SET debug_disable_indexscan_prefetch = "
                        f"{disable_prefetch_guc_value(prefetch)}")


def check_plan(conn, sql):
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (COSTS OFF) {sql}")
        plan = "\n".join(r[0] for r in cur.fetchall())
    if "Nested Loop Anti Join" not in plan or "Index Scan using anti_inner_k_idx" not in plan:
        sys.exit(f"Error: unexpected plan, the reproducer needs a nestloop anti join "
                 f"over a plain index scan:\n{plan}")
    return plan


def explain_analyze(conn, sql):
    """Run under EXPLAIN (ANALYZE, BUFFERS); return (exec_ms, inner_read, inner_hit, total_read)."""
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) {sql}")
        lines = [r[0] for r in cur.fetchall()]
    exec_ms = inner_read = inner_hit = total_read = None
    in_inner = False
    for line in lines:
        m = re.search(r"Execution Time: ([\d.]+) ms", line)
        if m:
            exec_ms = float(m.group(1))
        if "Index Scan using anti_inner_k_idx" in line:
            in_inner = True
        m = re.search(r"Buffers: shared(?: hit=(\d+))?(?: read=(\d+))?", line)
        if m:
            hit = int(m.group(1) or 0)
            read = int(m.group(2) or 0)
            if total_read is None:
                total_read = read          # the first Buffers line is the top node's
            if in_inner and inner_read is None:
                inner_read, inner_hit = read, hit
    if exec_ms is None or inner_read is None:
        sys.exit("Error: could not parse EXPLAIN output:\n" + "\n".join(lines))
    return exec_ms, inner_read, inner_hit, total_read


def measure(label, bin_dir, data_dir, conn_details, args, log_file, has_prefetch_guc, prefetch):
    """Cold-start the server and run the query args.runs times; return per-run rows."""
    print(f"\n===== {label} =====")
    start_server(bin_dir, data_dir, conn_details, args, log_file)
    if args.drop_os_cache:
        clear_os_cache()
    conn = connect(conn_details)
    set_gucs(conn, has_prefetch_guc, prefetch)
    sql = anti_query(args)
    check_plan(conn, sql)
    rows = []
    for i in range(args.runs):
        exec_ms, inner_read, inner_hit, total_read = explain_analyze(conn, sql)
        tag = "cold" if i == 0 else "warm"
        print(f"  run {i + 1} ({tag}): {exec_ms:9.1f} ms   inner scan: read={inner_read:7d} "
              f"hit={inner_hit:7d}   query total read={total_read}")
        rows.append((exec_ms, inner_read, total_read))
    conn.close()
    return rows


def main():
    args = parse_arguments()
    bin_dir, data_dir, _source_dir, conn_details = SERVERS[args.server]
    has_prefetch_guc = BUILD_HAS_PREFETCH[args.server]
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prefetch_results", f"antijoin_{args.server}.postgres_log")

    print(f"Server: {args.server} ({bin_dir}, {data_dir}, port {conn_details['port']})")

    # First start: make sure the data exists, then describe it and the plan.
    start_server(bin_dir, data_dir, conn_details, args, log_file)
    conn = connect(conn_details)
    ensure_data(conn, args)
    describe_data(conn, args)
    set_gucs(conn, has_prefetch_guc, "on")
    print("\nPlan:\n" + check_plan(conn, anti_query(args)))
    conn.close()

    if has_prefetch_guc:
        settings = ["on", "off"] if args.prefetch == "both" else [args.prefetch]
        configs = [(f"{args.server}, debug_disable_indexscan_prefetch = "
                    f"{disable_prefetch_guc_value(s)} (prefetch {s})", s)
                   for s in settings]
    else:
        configs = [(f"{args.server} (no prefetch GUC)", None)]

    # Each configuration starts from a cold buffer pool: the unfixed code only
    # loses if its abandoned read-ahead gets to evict the working set before
    # that working set is fully resident, and a prefetch=off run would leave
    # exactly the useful blocks behind for whatever follows it.
    results = []
    for label, prefetch in configs:
        rows = measure(label, bin_dir, data_dir, conn_details, args, log_file,
                       has_prefetch_guc, prefetch)
        results.append((label, rows))

    print("\n===== summary (warm = median over the runs after the cold first run) =====")
    print(f"{'configuration':44s} {'cold ms':>9s} {'warm ms':>9s} "
          f"{'warm reads':>11s} {'/rescan':>8s} {'inner node reads':>17s}")
    for label, rows in results:
        cold_ms = rows[0][0]
        warm = rows[1:] or rows
        warm_ms = median(r[0] for r in warm)
        inner_reads = median(r[1] for r in warm)
        total_reads = median(r[2] for r in warm)
        print(f"{label:44s} {cold_ms:9.1f} {warm_ms:9.1f} {total_reads:11.0f} "
              f"{total_reads / args.outer_rows:8.2f} {inner_reads:17.0f}")

    if not args.keep_running:
        stop_server(bin_dir, data_dir)
        print(f"\nServer stopped (it was started with shared_buffers={args.shared_buffers}, "
              f"io_method={args.io_method}).")


if __name__ == "__main__":
    main()
