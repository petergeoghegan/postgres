#!/usr/bin/env python3

"""
Performance profiling script for PostgreSQL using perf and FlameGraph.

This script automates the process of profiling two different versions of
PostgreSQL (e.g., 'master' and a 'patched' version) to generate
differential flame graphs for performance analysis of a specific SQL query.

It performs the following steps:
1.  Checks for necessary command-line tool dependencies (`perf`, `git`).
2.  Ensures the FlameGraph repository is available.
3.  For each PostgreSQL version ('master' and 'patch'):
    a. Starts the PostgreSQL server.
    b. Waits for the server to become available.
    c. Connects to the database and retrieves the backend process PID.
    d. Prewarms database caches.
    e. Starts `perf record` to sample the backend process.
    f. Executes a specified SQL query repeatedly.
    g. Stops `perf` and the PostgreSQL server.
    h. Generates a normalized stack trace file.
4.  Uses the FlameGraph toolkit to:
    a. Fold the stack traces from both profiling runs.
    b. Generate individual flame graphs for both versions.
    c. Generate a differential flame graph comparing the two versions.

Configuration:
The script requires configuration of paths, connection details, and profiling
parameters in the "--- Configuration ---" section of the script. This includes:
- Paths to PostgreSQL binaries and data directories for both versions.
- Connection details.
- The SQL query to be profiled.
- `perf` sampling frequency.
- Path to the FlameGraph repository.

Output:
The script creates an 'output_perf_flamegraph' directory (by default)
containing:
- Log files for each PostgreSQL server instance.
- Raw `perf.data` files (named 'master' and 'patch').
- Collapsed stack files (`.stacks` and `.folded`).
- Individual SVG flame graphs for each version.
- A differential SVG flame graph (`diff.svg`).

CPU-specific perf-stat counter plan (AMD Zen 3 / Ryzen 9 5950X):
The --perfstat path's event groups and the rate formulas derived from them are
SPECIFIC TO AMD Zen 3 (Ryzen 9 5950X).  Only 4 general-purpose core PMCs are
usable on this part (verified: requesting a 5th programmable event forces the
kernel to multiplex, so each event runs only ~80% of the window and the totals
become unreliable), so events are measured in passes of at most 4 (see
PERFSTAT_PASSES).  We use the native AMD events (ic_tag_hit_miss.*,
bp_l1_tlb_*, ls_*) rather than perf's generic aliases because the generic
aliases measure DIFFERENT cache levels on AMD: e.g. the generic
"dTLB-load-misses" maps to an L2/page-walk miss, not the L1-DTLB miss we want.
Intel Top-down / TMA analysis is deliberately NOT used here: it is unsupported
on this AMD part (`perf stat --topdown` reports "topdown metric groups aren't
present").  To retarget another CPU, the event groups below and the formulas in
display_perfstat_comparison() must be revisited.
"""

import argparse
from collections import OrderedDict
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time

import psycopg

from prefetch_benchmark import (
    MASTER_BIN, PATCH_BIN,
    MASTER_DATA_DIR, PATCH_DATA_DIR,
    MASTER_CONN, PATCH_CONN,
    BASELINE_CONFIGS, TESTBRANCH_CONFIGS, BUILD_HAS_PREFETCH,
    clear_os_cache, evict_relations, prewarm_relations,
    set_gucs, reset_gucs,
    setup_tmpfs_hugepages, copy_binaries_to_tmpfs, cleanup_tmpfs,
    kill_interfering_processes, check_benchmark_env,
)

from prefetch_benchmark import (
    BENCHMARK_SUITES, time_discard,
    # Reuse server startup + io knobs verbatim so this script measures under the
    # exact same conditions prefetch_benchmark creates (no divergence to drift).
    start_server, stop_server, pin_backend,
    add_io_server_args, resolve_io_method_defaults,
)

# Build combined query lookup and per-query data functions from
# BENCHMARK_SUITES (the single source of truth shared with
# prefetch_benchmark.py and patch_report.py).
ALL_QUERIES = OrderedDict()
_QUERY_DATA_FNS = {}   # query_id -> (verify_fn, load_fn)

for _suite in BENCHMARK_SUITES:
    _verify = _suite["verify_fn"]
    _load = _suite["load_fn"]
    for _qid, _qdef in _suite["queries"].items():
        ALL_QUERIES[_qid] = _qdef
        _QUERY_DATA_FNS[_qid] = (_verify, _load)

def _get_data_functions(query_id):
    """Return (verify_fn, load_fn) for the given query ID."""
    return _QUERY_DATA_FNS[query_id]

# os.environ["MALLOPT_TOP_PAD_"] = str(64 * 1024 * 1024)
# os.environ["MALLOPT_TOP_PAD"] = str(64 * 1024 * 1024)
# os.environ["M_TOP_PAD"] = str(64 * 1024 * 1024)
# os.environ["M_MMAP_THRESHOLD"] = str(64 * 1024 * 1024)
# os.environ["M_TRIM_THRESHOLD"] = str(64 * 1024 * 1024)
# os.environ["M_MMAP_MAX"] = str(0)
# os.environ["M_ARENA_TEST"] = str(64)

# --- Configuration ---

# Use MASTER_CONN and PATCH_CONN from prefetch_benchmark, aliased for compatibility.
# These are overridden in main() when --baseline is not "master".
MASTER_CONN_DETAILS = MASTER_CONN
PATCH_CONN_DETAILS = PATCH_CONN

# The frequency of 'perf' sampling.
PERF_FREQUENCY=9999

# --- Script variables ---
FLAMEGRAPH_DIR="/home/pg/code/FlameGraph"
OUTPUT_DIR = "output_perf_flamegraph"

# Number of timed query executions measured per perf-stat pass.  Constant so the
# absolute counter totals are directly comparable between master and patch.
PERFSTAT_REPS = 20

# perf-stat event plan, SPECIFIC TO AMD Zen 3 (Ryzen 9 5950X): 4 usable GP
# counters here, so <=4 events per pass; the constant workload is replayed once
# per pass.  Native AMD events (generic aliases measure different cache levels).
PERFSTAT_PASSES = [
    ("base+branch", "instructions,cpu-cycles,ex_ret_brn,ex_ret_brn_misp"),
    ("icache",      "ic_tag_hit_miss.all_instruction_cache_accesses,ic_tag_hit_miss.instruction_cache_hit,ic_tag_hit_miss.instruction_cache_miss"),
    ("itlb",        "bp_l1_tlb_fetch_hit,bp_l1_tlb_miss_l2_tlb_hit,bp_l1_tlb_miss_l2_tlb_miss"),
    ("dcache+dtlb", "ls_dc_accesses,L1-dcache-load-misses,ls_l1_d_tlb_miss.all,ls_l1_d_tlb_miss.tlb_reload_4k_l2_miss"),
]

# --- Benchmark configurations ---
BENCHMARKS = {
    "nestloop": {
        "sql_query": "select count(*) from pgbench_accounts a join pgbench_branches b on a.bid = b.bid",
        "query_repetitions": 5,
    },
    "simple_select": {
        "sql_query": "select * from pgbench_accounts where aid = %s::int4",
        "query_repetitions": 500_000,
        "max_aid_val": 100_000,
    },
    "bitmap": {
        "sql_query": "SELECT count(abalance) FROM pgbench_accounts WHERE aid BETWEEN %s AND %s",
        "query_repetitions": 500_000,
        "bitmap_range": 2000,
    },
}

def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Performance profiling script for PostgreSQL using perf and FlameGraph.",
        allow_abbrev=False
    )
    parser.add_argument(
        "--pgbench-scale",
        type=int,
        default=10,
        help="pgbench scale factor for initialization (default: 10)"
    )
    parser.add_argument(
        "--skip-pgbench-init", "--skip",
        action="store_true",
        dest="skip_pgbench_init",
        help="Skip pgbench initialization"
    )
    parser.add_argument(
        "--perf",
        nargs="?",
        const=True,
        default=False,
        metavar="EVENT",
        help="Run perf profiling (disabled by default). Optionally specify event name (e.g., --perf cycles, --perf cache-misses)"
    )
    parser.add_argument(
        "--perfstat",
        action="store_true",
        help="Run perf stat instead of perf record (mutually exclusive with --perf)"
    )
    parser.add_argument(
        "--benchmark",
        choices=["nestloop", "simple_select", "bitmap"],
        default="nestloop",
        help="Benchmark to run (default: nestloop)"
    )
    parser.add_argument(
        "--patch-first",
        action="store_true",
        help="Profile patch version before master (instead of after)"
    )
    parser.add_argument(
        "--highfreq",
        action="store_true",
        help="Use high sampling frequency for perf (49999 Hz instead of 9999 Hz)"
    )
    parser.add_argument(
        "--num-queries",
        type=int,
        dest="num_queries",
        help="Number of queries to execute (only valid with --benchmark simple_select or bitmap)"
    )
    parser.add_argument(
        "--bitmap",
        action="store_const",
        const="bitmap",
        dest="benchmark",
        help="Shorthand for --benchmark bitmap (bitmap index scan profiling)"
    )
    parser.add_argument(
        "--bitmap-range",
        type=int,
        default=2000,
        help="Range size for bitmap benchmark (default: 2000 contiguous items)"
    )
    parser.add_argument(
        "--ios",
        action="store_true",
        dest="index_only_scan",
        help="Force index-only scans instead of plain index scans"
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        dest="use_hash_index",
        help="Use a hash index instead of the default B-tree index"
    )
    parser.add_argument(
        "--no-tmpfs-hugepages",
        action="store_true",
        help="Disable tmpfs with huge=always (enabled by default)"
    )
    parser.add_argument(
        "--discard-runs",
        type=int,
        default=3,
        help="Number of initial query runs to discard before starting measurement (default: 3)"
    )
    parser.add_argument(
        "--benchmark-cpu",
        type=int,
        default=14,
        help="CPU core to pin PostgreSQL backend to (default: 14, a performance core)"
    )
    parser.add_argument(
        "--perf-cpu",
        type=int,
        default=15,
        help="CPU core to pin perf process to (default: 15)"
    )
    parser.add_argument(
        "--disable-prefetch",
        action="store_true",
        help="Disable hardware prefetchers on benchmark CPU during test (requires passwordless sudo for wrmsr)"
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        dest="queries",
        help="Run a prefetch benchmark query by id (e.g., Q1, A8), named to match "
             "prefetch_benchmark's --queries. Takes a single query id. Mutually "
             "exclusive with --benchmark."
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help="Run in cached mode (prewarm all relations). Only applicable with --queries."
    )
    parser.add_argument(
        "--skip-data-load",
        action="store_true",
        help="Skip prefetch benchmark data verification/loading (use with --queries)"
    )
    # Pinning + SCHED_FIFO is OFF by default, matching prefetch_benchmark (which
    # found SCHED_FIFO can starve io_uring and skew prefetch results).  --pin opts
    # in; --no-pin is the explicit (and now default) off.
    parser.add_argument(
        "--pin",
        action="store_true",
        dest="pin",
        help="Enable CPU pinning + SCHED_FIFO on the backend (off by default, "
             "matches prefetch_benchmark; can starve io_uring)"
    )
    parser.add_argument(
        "--no-pin",
        action="store_true",
        dest="no_pin",
        help="Force pinning off (the default; uses perf -p PID instead of perf -C)"
    )
    # Server-start io GUC knobs, shared verbatim with prefetch_benchmark.
    add_io_server_args(parser)
    prefetch_group = parser.add_mutually_exclusive_group()
    prefetch_group.add_argument(
        "--prefetch-only",
        action="store_true",
        dest="prefetch_only",
        help="Set enable_indexscan_prefetch=on on patch (use with --queries)"
    )
    prefetch_group.add_argument(
        "--prefetch-disabled",
        action="store_true",
        dest="prefetch_disabled",
        help="Set enable_indexscan_prefetch=off on patch (use with --queries)"
    )
    parser.add_argument(
        "--baseline",
        type=str,
        choices=list(BASELINE_CONFIGS.keys()),
        default="master",
        help="Baseline PostgreSQL build to compare against (default: master)"
    )
    parser.add_argument(
        "--testbranch",
        type=str,
        choices=list(TESTBRANCH_CONFIGS.keys()),
        default="patch",
        help="PostgreSQL build to test (default: patch). Use 'master' to compare master vs rel18."
    )
    return parser.parse_args()

def check_dependencies():
    """Check if required tools are available."""
    for cmd in ["perf", "git"]:
        if not shutil.which(cmd):
            print(f"Error: Command '{cmd}' not found. Please install it.")
            sys.exit(1)
    if not os.path.isdir(FLAMEGRAPH_DIR):
        raise FileNotFoundError("no FlameGraph repository")

def validate_perf_events(event_spec):
    """Fail fast on a bad --perf event BEFORE paying for the whole profiling run.

    `perf record -e <event>` only validates the event after it has launched, mid
    way through the (already expensive) server start + prewarm + workload; a typo
    like `--perf icache` (the real AMD event is ic_tag_hit_miss.*) then surfaces
    much later as an empty perf.data and a flame-graph failure.  `perf stat -e
    <spec> -- true` checks the identical event string cheaply up front: it exits 0
    for a usable group and non-zero (printing "Bad event name") otherwise.
    *event_spec* may be the comma-separated list passed straight to `record -e`.
    """
    proc = subprocess.run(
        ["perf", "stat", "-e", event_spec, "--", "true"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(f"Error: invalid perf event(s) '{event_spec}' for `perf record -e`:")
        for line in proc.stderr.splitlines():
            if line.strip():
                print(f"  {line}")
        print("Run 'perf list' for the events available on this CPU.")
        sys.exit(1)

def disable_prefetchers(cpu_core):
    """
    Disable hardware prefetchers on the specified CPU core.
    Returns the original MSR value so it can be restored later.

    For AMD Zen CPUs, MSR 0xC0011022 [DC Configuration Register] controls prefetchers:
    - Bit 13 (0x2000): DisHwPf - Disable hardware data prefetcher
    - Bit 15 (0x8000): DisWcPf - Disable prefetcher for Write Combining stores

    We set bit 13 to disable the main hardware prefetcher.

    Requires passwordless sudo for wrmsr/rdmsr.
    """
    MSR_DC_CFG = "0xC0011022"
    DISABLE_HW_PREFETCH_BIT = 0x2000  # Bit 13

    try:
        # Read current value
        result = subprocess.run(
            ["sudo", "rdmsr", "-p", str(cpu_core), MSR_DC_CFG],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            print(f"Warning: Could not read prefetcher MSR: {result.stderr}")
            return None

        original_value_str = result.stdout.strip()
        original_value = int(original_value_str, 16)
        print(f"Current DC_CFG MSR value on CPU {cpu_core}: 0x{original_value:x}")

        # Check if hardware prefetcher is currently enabled
        if original_value & DISABLE_HW_PREFETCH_BIT:
            print(f"  -> Hardware prefetcher is already DISABLED (bit 13 is set)")
        else:
            print(f"  -> Hardware prefetcher is currently ENABLED (bit 13 is clear)")

        # Set bit 13 to disable hardware prefetcher
        new_value = original_value | DISABLE_HW_PREFETCH_BIT

        result = subprocess.run(
            ["sudo", "wrmsr", "-p", str(cpu_core), MSR_DC_CFG, f"0x{new_value:x}"],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            print(f"Warning: Could not disable prefetchers: {result.stderr}")
            return None

        # Verify the change
        result = subprocess.run(
            ["sudo", "rdmsr", "-p", str(cpu_core), MSR_DC_CFG],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0:
            verify_value = int(result.stdout.strip(), 16)
            print(f"Hardware prefetcher disabled on CPU {cpu_core}. New MSR value: 0x{verify_value:x}")
            if verify_value & DISABLE_HW_PREFETCH_BIT:
                print(f"  -> Verified: Bit 13 is now SET (prefetcher disabled)")
            else:
                print(f"  -> Warning: Bit 13 is still CLEAR (prefetcher may still be enabled!)")

        return original_value_str

    except Exception as e:
        print(f"Warning: Failed to disable prefetchers: {e}")
        return None

def restore_prefetchers(cpu_core, original_value):
    """Restore hardware prefetchers to their original state."""
    if original_value is None:
        return

    MSR_HWCR = "0xC0011022"

    try:
        result = subprocess.run(
            ["sudo", "wrmsr", "-p", str(cpu_core), MSR_HWCR, f"0x{original_value}"],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0:
            print(f"Restored prefetcher MSR on CPU {cpu_core} to original value: 0x{original_value}")
        else:
            print(f"Warning: Could not restore prefetchers: {result.stderr}")

    except Exception as e:
        print(f"Warning: Failed to restore prefetchers: {e}")

# setup_tmpfs_hugepages, copy_binaries_to_tmpfs, cleanup_tmpfs
# are imported from prefetch_benchmark


# start_server / stop_server are imported from prefetch_benchmark so this script
# starts servers with the IDENTICAL GUCs (io_method, effective_io_concurrency,
# max_parallel_workers_per_gather=0, ...).  A local copy previously diverged --
# it set only --autovacuum=off, so perf_flamegraph measured the patch under the
# cluster's default io config instead of the benchmark's, flipping results.

def init_pgbench(pg_bin_dir, conn_details, scale, use_hash_index=False):
    """Initialize pgbench database with specified scale factor."""
    print(f"Initializing pgbench with scale factor {scale}...")
    pgbench_path = os.path.join(pg_bin_dir, "pgbench")

    subprocess.run(
        [pgbench_path, "-i", "-s", str(scale),
         f"--host={conn_details['host']}",
         f"--port={conn_details['port']}",
         f"--user={conn_details['user']}",
         conn_details["dbname"]],
        check=True
    )
    print(f"pgbench initialization completed.")

    if use_hash_index:
        print("Creating hash indexes on pgbench_accounts.aid and pgbench_branches.bid...")
        try:
            conn = psycopg.connect(**conn_details, prepare_threshold=0)
            with conn.cursor() as cursor:
                # Drop the primary key constraint on pgbench_accounts (which uses the B-tree index)
                cursor.execute("ALTER TABLE pgbench_accounts DROP CONSTRAINT pgbench_accounts_pkey;")
                # Create hash index on pgbench_accounts.aid
                cursor.execute("CREATE INDEX pgbench_accounts_aid_hash ON pgbench_accounts USING hash (aid);")
                # Drop the primary key constraint on pgbench_branches
                cursor.execute("ALTER TABLE pgbench_branches DROP CONSTRAINT pgbench_branches_pkey;")
                # Create hash index on pgbench_branches.bid
                cursor.execute("CREATE INDEX pgbench_branches_bid_hash ON pgbench_branches USING hash (bid);")
            conn.commit()
            conn.close()
            print("Hash indexes created successfully.")
        except Exception as e:
            print(f"Error creating hash indexes: {e}")
            sys.exit(1)

def get_pgbench_row_count(pg_name, conn_details):
    """Get the actual number of rows in pgbench_accounts table."""
    try:
        conn = psycopg.connect(**conn_details, prepare_threshold=0)
        with conn.cursor() as cursor:
            cursor.execute("select count(*) from pgbench_accounts")
            count = cursor.fetchone()[0]
        conn.close()
        print(f"{pg_name}: pgbench_accounts has {count} rows")
        return count
    except Exception as e:
        print(f"Error getting row count from {pg_name}: {e}")
        sys.exit(1)

def verify_hash_index_exists(pg_name, conn_details):
    """Verify that hash indexes exist on pgbench_accounts.aid and pgbench_branches.bid."""
    try:
        conn = psycopg.connect(**conn_details, prepare_threshold=0)
        with conn.cursor() as cursor:
            # Check for hash index on pgbench_accounts.aid
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_accounts'
                  AND indexdef LIKE '%USING hash%'
            """)
            accounts_result = cursor.fetchone()

            # Check for hash index on pgbench_branches.bid
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_branches'
                  AND indexdef LIKE '%USING hash%'
            """)
            branches_result = cursor.fetchone()
        conn.close()

        if accounts_result is None:
            print(f"Error: {pg_name}: No hash index found on pgbench_accounts")
            print(f"When using --hash with --skip, hash indexes must be present.")
            sys.exit(1)

        if branches_result is None:
            print(f"Error: {pg_name}: No hash index found on pgbench_branches")
            print(f"When using --hash with --skip, hash indexes must be present.")
            sys.exit(1)

        print(f"{pg_name}: Verified hash indexes exist: {accounts_result[0]}, {branches_result[0]}")
        return True
    except Exception as e:
        print(f"Error verifying hash indexes on {pg_name}: {e}")
        sys.exit(1)

def verify_btree_index_exists(pg_name, conn_details):
    """Verify that B-tree indexes exist on pgbench_accounts and pgbench_branches when --hash is not used."""
    try:
        conn = psycopg.connect(**conn_details, prepare_threshold=0)
        with conn.cursor() as cursor:
            # Check for B-tree index on pgbench_accounts (or no index spec, which defaults to B-tree)
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_accounts'
                  AND indexdef NOT LIKE '%USING hash%'
            """)
            accounts_result = cursor.fetchone()

            # Check for B-tree index on pgbench_branches
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_branches'
                  AND indexdef NOT LIKE '%USING hash%'
            """)
            branches_result = cursor.fetchone()
        conn.close()

        if accounts_result is None:
            print(f"Error: {pg_name}: No B-tree index found on pgbench_accounts")
            print(f"When not using --hash with --skip, B-tree indexes must be present.")
            sys.exit(1)

        if branches_result is None:
            print(f"Error: {pg_name}: No B-tree index found on pgbench_branches")
            print(f"When not using --hash with --skip, B-tree indexes must be present.")
            sys.exit(1)

        print(f"{pg_name}: Verified B-tree indexes exist: {accounts_result[0]}, {branches_result[0]}")
        return True
    except Exception as e:
        print(f"Error verifying B-tree indexes on {pg_name}: {e}")
        sys.exit(1)

def profile_postgres(pg_bin_dir, pg_name, conn_details, output_file, sql_query, query_repetitions, run_perf, max_aid_val=None, perf_event=None, highfreq=False, index_only_scan=False, run_perfstat=False, discard_runs=0, benchmark_cpu=14, perf_cpu=15, disable_prefetch=False, is_master=False, prefetch_setting=None, no_pin=False, use_bitmap=False, bitmap_range=2000):
    """Profiles a PostgreSQL instance (assumes server is already running)."""
    print(f"--- Testing {pg_name} ---")

    conn = None
    try:
        # Connect to the database to get the backend PID
        conn = psycopg.connect(**conn_details, prepare_threshold=0)

        backend_pid = conn.info.backend_pid
        print(f"Successfully connected. Backend PID is: {backend_pid}")

        # Pin the backend process to a specific core and use RT scheduling to prevent migration.
        if not no_pin:
            try:
                # Pin backend to specified core using taskset (doesn't require root)
                result = subprocess.run(
                    ["taskset", "-cp", str(benchmark_cpu), str(backend_pid)],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode == 0:
                    print(f"Pinned backend PID {backend_pid} to CPU {benchmark_cpu}.")
                else:
                    print(f"Warning: Could not set CPU affinity: {result.stderr}")

                # Set RT scheduling (SCHED_FIFO) to truly pin and prevent migrations
                # Use chrt with sudo for RT scheduling (requires passwordless sudo or password entry)
                result = subprocess.run(
                    ["sudo", "chrt", "-f", "-p", "1", str(backend_pid)],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode == 0:
                    print(f"Set backend PID {backend_pid} to SCHED_FIFO priority 1 (prevents CPU migration).")

                    # Verify RT scheduling was actually set
                    verify_result = subprocess.run(
                        ["chrt", "-p", str(backend_pid)],
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    if verify_result.returncode == 0:
                        print(f"Verification: {verify_result.stdout.strip()}")
                    else:
                        print(f"Warning: Could not verify RT scheduling")
                else:
                    print(f"Warning: Could not set RT scheduling: {result.stderr}")
                    print(f"         Configure passwordless sudo for 'chrt' or run entire script with sudo.")
            except Exception as e:
                print(f"Warning: Could not set CPU affinity/RT scheduling for backend PID {backend_pid}: {e}")
        else:
            print("CPU pinning disabled (--no-pin)")

        # Disable hardware prefetchers if requested
        original_prefetch_value = None
        if disable_prefetch:
            print(f"\nDisabling hardware prefetchers on CPU {benchmark_cpu}...")
            original_prefetch_value = disable_prefetchers(benchmark_cpu)
            if original_prefetch_value is None:
                print("If you don't have passwordless sudo configured, you can manually:")
                print(f"  1. Read current value: sudo rdmsr -p {benchmark_cpu} 0xC0011022")
                print(f"  2. Set bit 13 to disable: sudo wrmsr -p {benchmark_cpu} 0xC0011022 <original_value | 0x2000>")
                print(f"  3. After benchmark, restore: sudo wrmsr -p {benchmark_cpu} 0xC0011022 <original_value>")

        print("Prewarming...")
        with conn.cursor() as cursor:
            # if pg_name == "patch":
            #     cursor.execute("set enable_indexscan_prefetch=off;")
            if use_bitmap:
                cursor.execute("set enable_bitmapscan=on;")
                cursor.execute("set enable_indexscan=off;")
                cursor.execute("set enable_indexonlyscan=off;")
            else:
                cursor.execute("set enable_bitmapscan=off;")
                if index_only_scan:
                    cursor.execute("set enable_indexonlyscan=on;")
                else:
                    cursor.execute("set enable_indexonlyscan=off;")
            cursor.execute("set enable_hashjoin=off;")
            cursor.execute("set enable_material=off;")
            cursor.execute("set enable_memoize=off;")
            cursor.execute("set enable_mergejoin=off;")
            cursor.execute("set enable_seqscan=off;")

            cursor.execute("set max_parallel_workers_per_gather = 0;")
            # Set prefetch GUC only on patch (not master)
            if not is_master and prefetch_setting is not None:
                cursor.execute(f"set enable_indexscan_prefetch = {prefetch_setting};")
            cursor.execute("create extension if not exists pg_prewarm;")
            cursor.execute("select pg_prewarm('pgbench_accounts');")
            # Prewarm all indexes on pgbench_accounts
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_accounts'
            """)
            for (index_name,) in cursor.fetchall():
                cursor.execute(f"select pg_prewarm('{index_name}');")
            cursor.execute("select pg_prewarm('pgbench_branches');")
            # Prewarm all indexes on pgbench_branches
            cursor.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'pgbench_branches'
            """)
            for (index_name,) in cursor.fetchall():
                cursor.execute(f"select pg_prewarm('{index_name}');")
        print("Finished prewarming")

        # Show the query text and EXPLAIN plan before starting the benchmark
        print("\n--- Query Plan (EXPLAIN) ---")
        print(sql_query)
        print()
        with conn.cursor() as cursor: # type: ignore
            explain_query = f"EXPLAIN {sql_query}"
            if use_bitmap and max_aid_val is not None:
                aid = random.randint(1, max_aid_val - bitmap_range)
                cursor.execute(explain_query, params=[aid, aid + bitmap_range - 1], prepare=False)
            elif max_aid_val is not None:
                cursor.execute(explain_query, params=[random.randint(1, max_aid_val)], prepare=False)
            else:
                cursor.execute(explain_query, prepare=False)
            for row in cursor.fetchall():
                print(row[0])
        print("--- End Query Plan ---\n")

        # Execute the query repeatedly in the same connection
        total_runs = query_repetitions + discard_runs
        if discard_runs > 0:
            print(f"Executing the SQL query {total_runs} times (discarding first {discard_runs} runs)...")
        else:
            print(f"Executing the SQL query {query_repetitions} times...")
        random.seed(42)

        # Execute discard runs before starting measurement
        if discard_runs > 0:
            print(f"Warming up: executing {discard_runs} discard runs...")
            with conn.cursor() as cursor:  # type: ignore
                for i in range(discard_runs):
                    if use_bitmap and max_aid_val is not None:
                        aid = random.randint(1, max_aid_val - bitmap_range)
                        cursor.execute(query=sql_query,
                                       params=[aid, aid + bitmap_range - 1],
                                       prepare=True)
                    elif max_aid_val is not None:
                        cursor.execute(query=sql_query,
                                       params=[random.randint(1, max_aid_val)],
                                       prepare=True)
                    else:
                        cursor.execute(query=sql_query, prepare=True)
            print(f"Warmup complete. Starting measurement...")

        # Start perf profiling just before the measured query loop
        perf_process = None
        perf_command = []
        start_time = time.time()

        if run_perfstat:
            # Curated AMD Zen 3 event passes over a constant workload.  This both
            # measures and times the query (run_perfstat_passes replays it), so
            # the normal measured loop below is skipped for this mode.  The
            # pgbench queries are parameterized, so run_one re-executes the
            # prepared statement once (no EXPLAIN ANALYZE) rather than using
            # time_discard (whose fixed EXECUTE form takes no per-call params).
            stat_json = OUTPUT_DIR + "/" + pg_name + "_perfstat.json"

            def run_one():
                with conn.cursor() as cur:  # type: ignore
                    if use_bitmap and max_aid_val is not None:
                        aid = random.randint(1, max_aid_val - bitmap_range)
                        cur.execute(query=sql_query,
                                    params=[aid, aid + bitmap_range - 1],
                                    prepare=True)
                    elif max_aid_val is not None:
                        cur.execute(query=sql_query,
                                    params=[random.randint(1, max_aid_val)],
                                    prepare=True)
                    else:
                        cur.execute(query=sql_query, prepare=True)

            print(f"Running perf-stat passes ({len(PERFSTAT_PASSES)} passes x "
                  f"{PERFSTAT_REPS} reps)...")
            run_perfstat_passes(conn, run_one, backend_pid, stat_json, perf_cpu)
            # Also grab perf's own default `perf stat` report (the familiar view).
            run_perf_stat_native(run_one, backend_pid,
                                 OUTPUT_DIR + "/" + pg_name + "_perfstat_native.txt",
                                 perf_cpu)
            total_time = time.time() - start_time
            print(f"perf-stat passes finished in \033[1m{total_time:.3f} seconds\033[0m")
        else:
            if run_perf:
                perf_freq = "49999" if highfreq else str(PERF_FREQUENCY)
                perf_command = [
                    "perf", "record",
                    "-F", perf_freq,
                ]
                # Only add -e flag if a specific perf event was requested
                if perf_event:
                    perf_command.extend(["-e", perf_event])

                if no_pin:
                    print(f"Starting perf on PID {backend_pid}...")
                    perf_command.extend([
                        "-p", str(backend_pid),
                        "-g",
                        "-o",  OUTPUT_DIR + "/" + pg_name,
                    ])
                else:
                    print(f"Starting perf on CPU core {benchmark_cpu}...")
                    perf_command.extend([
                        "-C", str(benchmark_cpu),  # Monitor CPU core (works with all event types)
                        "-g",
                        "-o",  OUTPUT_DIR + "/" + pg_name,
                    ])
                perf_process = subprocess.Popen(perf_command)

                # Pin perf process to a different core to minimize interference.
                if not no_pin:
                    try:
                        os.sched_setaffinity(perf_process.pid, {perf_cpu})
                        print(f"Pinned perf PID {perf_process.pid} to CPU {perf_cpu}.")
                    except (AttributeError, PermissionError, OSError) as e:
                        print(f"Warning: Could not set CPU affinity for perf PID {perf_process.pid}: {e}")

                # Give perf a moment to initialize before starting the workload
                time.sleep(1)

                # perf record validates -e lazily and exits immediately on a bad
                # event or permission problem; catch that here instead of SIGINT-ing
                # a dead process and emitting an empty stack trace / flame graph.
                if perf_process.poll() is not None:
                    raise RuntimeError(
                        f"perf record exited immediately (rc={perf_process.returncode}); "
                        f"command: {' '.join(perf_command)}")

            with conn.cursor() as cursor: # type: ignore
                # Show per-query timing for small query counts (nestloop benchmark)
                show_per_query_timing = query_repetitions <= 10

                for i in range(query_repetitions):
                    query_start = time.time()
                    if use_bitmap and max_aid_val is not None:
                        aid = random.randint(1, max_aid_val - bitmap_range)
                        cursor.execute(query=sql_query,
                                       params=[aid, aid + bitmap_range - 1],
                                       prepare=True)
                    elif max_aid_val is not None:
                        cursor.execute(query=sql_query,
                                       params=[random.randint(1, max_aid_val)],
                                       prepare=True)
                    else:
                        cursor.execute(query=sql_query, prepare=True)

                    if show_per_query_timing:
                        query_time = time.time() - query_start
                        print(f"  Query {i+1}/{query_repetitions}: {query_time:.3f} seconds")

            end_time = time.time()
            total_time = end_time - start_time
            print(f"Query loop finished in \033[1m{total_time:.3f} seconds\033[0m")

        # perf_process is set only for perf record; perfstat manages its own
        # subprocesses inside run_perfstat_passes.
        if perf_process:
            # Stop the perf process gracefully by sending SIGINT (like Ctrl+C)
            print("Stopping perf...")
            perf_process.send_signal(signal.SIGINT)

            # Wait for perf to terminate
            perf_process.wait()

            # Generate the stack trace file, normalizing the binary path
            print(f"Generating and normalizing stack trace file: {output_file}")

            # The sed expression will replace the full, version-specific path
            # with the generic name 'postgres', allowing difffolded.pl to match symbols.
            pg_executable_path = os.path.join(pg_bin_dir, "postgres")
            sed_expression = f"s|{pg_executable_path}|postgres|g"

            perf_script_process = subprocess.Popen(["perf", "script",
                                                    "-i",  OUTPUT_DIR + "/" + pg_name,
                                                    ], stdout=subprocess.PIPE)

            with open(output_file, "w") as f:
                subprocess.run(
                    ["sed", sed_expression],
                    stdin=perf_script_process.stdout,
                    stdout=f,
                    check=True
                )
            perf_script_process.stdout.close()
            perf_script_process.wait()

    finally:
        # Restore hardware prefetchers if they were disabled
        if disable_prefetch and original_prefetch_value is not None:
            print(f"\nRestoring hardware prefetchers on CPU {benchmark_cpu}...")
            restore_prefetchers(benchmark_cpu, original_prefetch_value)

        # Ensure the connection is closed
        if conn:
            conn.close()
        print("----------------------------------------")

    # Return string of perf command for flamegraph --subtitle arg
    return ' '.join(perf_command), total_time

def run_perfstat_passes(conn, run_one_query, backend_pid, stat_json_path, perf_cpu):
    """Measure the curated AMD Zen 3 event groups (PERFSTAT_PASSES) over a
    constant workload and write the merged per-event totals as JSON.

    *run_one_query* is a zero-arg callable that executes the query once with no
    instrumentation.  Each pass attaches `perf stat -p <pid>` for at most 4
    events (the usable GP-counter budget on this CPU), then replays the query
    PERFSTAT_REPS times back-to-back so the totals are comparable run to run.
    Cache preparation must already have happened before this is called -- it is
    NOT re-done between reps; this measures steady-state CPU behavior.

    Returns the merged dict {event_name: float_or_None}.  Missing/unsupported
    counters are stored as None.
    """
    merged = {}

    for label, events in PERFSTAT_PASSES:
        csv_path = stat_json_path + f".{label}.csv"
        perf_command = [
            "perf", "stat",
            "-x", ",",                  # CSV (machine-readable) output
            "-e", events,
            "-p", str(backend_pid),     # attach to the backend
            "-o", csv_path,
        ]
        print(f"  perf-stat pass '{label}': {events}")
        perf_process = subprocess.Popen(perf_command)

        # Pin perf to its own core (best-effort); ignore failures.
        try:
            os.sched_setaffinity(perf_process.pid, {perf_cpu})
        except (AttributeError, PermissionError, OSError):
            pass

        # Let perf attach.  With `-p pid` an idle attach window adds ~0 to the
        # per-thread counters, so the totals are dominated by the query itself.
        time.sleep(0.3)

        for _ in range(PERFSTAT_REPS):
            run_one_query()

        # SIGINT makes perf flush its -o file before exiting; SIGKILL would not,
        # silently losing the whole pass.  Retry SIGINT once, then give up loudly.
        perf_process.send_signal(signal.SIGINT)
        try:
            perf_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            perf_process.send_signal(signal.SIGINT)
            try:
                perf_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"  Warning: perf did not exit on SIGINT; killing -- "
                      f"pass '{label}' counts will be lost (shown as n/a)")
                perf_process.kill()
                perf_process.wait()

        # Parse the CSV produced by `-x ,`.  Each data line is
        # "value,unit,event,..."; value may be "<not counted>" /
        # "<not supported>" (stored as None).  Comment lines start with '#'.
        try:
            with open(csv_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    fields = line.split(",")
                    if len(fields) < 3:
                        continue
                    value_str = fields[0].strip()
                    event_name = fields[2].strip()
                    if not event_name:
                        continue
                    if value_str.startswith("<"):
                        merged[event_name] = None
                    else:
                        try:
                            merged[event_name] = float(value_str)
                        except ValueError:
                            merged[event_name] = None
        except FileNotFoundError:
            # perf writes no -o file if ANY event in the group is unrecognized on
            # this CPU, so the whole group drops out.  Say so (perf's own error
            # went to stderr above) rather than silently showing n/a.
            print(f"  Warning: perf produced no output for pass '{label}' -- the "
                  f"whole group is dropped (often one unrecognized event for this "
                  f"CPU); its rates will show n/a. Events: {events}")

    with open(stat_json_path, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)

    return merged


def write_perf_diff(baseline_data, test_data, out_path):
    """Run `perf diff <baseline> <test>` (master vs patch) and both print it and
    save it to *out_path* -- the function-level, per-event comparison.

    This replaces the old "here's the recipe, go run it yourself" printout.  perf
    diff is the most actionable view: it ranks symbols by how much each one moved
    between the two profiles.  Baseline is the first file (master), so the Delta
    column is patch - master.  With a multi-event recording (e.g. --perf
    cycles,instructions,branch-misses) perf diff prints one "# Event '<name>'"
    block per event, so you see where every event type shifted, per function."""
    try:
        proc = subprocess.run(
            ["perf", "diff", baseline_data, test_data],
            capture_output=True, text=True, check=False,
        )
    except Exception as e:
        print(f"Warning: could not run perf diff: {e}")
        return
    with open(out_path, "w") as f:
        f.write(proc.stdout)
    print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode != 0 and proc.stderr.strip():
        print(proc.stderr, end="")


def run_perf_stat_native(run_one_query, backend_pid, out_txt_path, perf_cpu):
    """Capture perf's OWN human-readable `perf stat` report for the backend over
    the constant workload, and return its text.

    The curated PERFSTAT_PASSES path harvests raw native AMD counts via `perf stat
    -x ,` and then reconstructs a custom table -- which is *why* it never looks
    like perf stat: the machine-readable CSV carries none of perf's derived `#`
    annotations, and an explicit `-e` list suppresses them anyway.  Here we run the
    DEFAULT event set (no -e) so perf computes and prints the familiar metrics
    (insn-per-cycle, GHz, branch-miss %).  Those defaults exceed the 4 GP counters
    on this part, so perf multiplexes and labels each event's share -- fine for an
    at-a-glance overview alongside the exact curated table."""
    perf_command = [
        "perf", "stat",
        "-p", str(backend_pid),     # backend thread only, like the curated passes
        "-o", out_txt_path,
    ]
    proc = subprocess.Popen(perf_command)
    try:
        os.sched_setaffinity(proc.pid, {perf_cpu})
    except (AttributeError, PermissionError, OSError):
        pass
    time.sleep(0.3)               # let perf attach
    for _ in range(PERFSTAT_REPS):
        run_one_query()
    # SIGINT (not SIGKILL) so perf flushes its -o report before exiting.
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    try:
        with open(out_txt_path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def display_perfstat_comparison(master_json, patch_json):
    """Display an AMD Zen 3 perf-stat comparison from the two JSON files written
    by run_perfstat_passes (master, patch).  Raw counts show a patch/master
    ratio; reconstructed hit/miss rates show master %, patch %, and the
    difference in percentage POINTS.  Missing/None/zero-denominator cells are
    printed as 'n/a'."""
    print("\n" + "="*100)
    print("PERF STAT COMPARISON: MASTER vs PATCH")
    print(f"(AMD Zen 3 native events, totals over {PERFSTAT_REPS} constant runs per pass)")
    print("="*100 + "\n")

    # First: perf's own default `perf stat` report for each version -- the familiar
    # view with insn-per-cycle / GHz / branch-miss %, which the curated CSV table
    # below cannot reproduce.  (Default events multiplex on this 4-counter part;
    # perf labels each event's share.)
    for name in ("master", "patch"):
        native_path = os.path.join(OUTPUT_DIR, f"{name}_perfstat_native.txt")
        try:
            with open(native_path, "r") as f:
                text = f.read()
        except FileNotFoundError:
            continue
        print(f"--- perf stat ({name}, perf's default events) ---")
        for line in text.splitlines():
            # Drop perf's "# started on ..." banner; keep the counters + metrics.
            if line.startswith("# started on"):
                continue
            print(line)
        print()

    try:
        with open(master_json, "r") as f:
            master = json.load(f)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: Could not load master perf stat JSON ({master_json}): {e}")
        return
    try:
        with open(patch_json, "r") as f:
            patch = json.load(f)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: Could not load patch perf stat JSON ({patch_json}): {e}")
        return

    def get(d, name):
        """Fetch an event value, returning None if missing or unmeasured."""
        v = d.get(name)
        return v if isinstance(v, (int, float)) else None

    def fmt_count(v):
        return f"{v:,.0f}" if v is not None else "n/a"

    def ratio(num, den):
        if num is None or den is None or den == 0:
            return None
        return num / den

    def fmt_ratio(r):
        return f"{r:.3f}x" if r is not None else "n/a"

    def rate(d, num_name, den_name):
        """100 * numerator / denominator, or None if not computable."""
        num = get(d, num_name)
        den = get(d, den_name)
        if num is None or den is None or den == 0:
            return None
        return 100.0 * num / den

    def fmt_pct(p):
        return f"{p:6.2f}%" if p is not None else "   n/a"

    def fmt_delta(m, p):
        if m is None or p is None:
            return "   n/a"
        return f"{p - m:+6.2f}pp"

    # --- Raw counts (ratio = patch / master) ---
    label_w = 22
    print(f"{'COUNT (raw)':<{label_w}} {'MASTER':>22} {'PATCH':>22} {'RATIO':>10}")
    print("-" * (label_w + 1 + 22 + 1 + 22 + 1 + 10))
    raw_counts = [
        ("instructions", "instructions"),
        ("cpu-cycles", "cpu-cycles"),
    ]
    for label, ev in raw_counts:
        mv = get(master, ev)
        pv = get(patch, ev)
        print(f"{label:<{label_w}} {fmt_count(mv):>22} {fmt_count(pv):>22} "
              f"{fmt_ratio(ratio(pv, mv)):>10}")

    # IPC (instructions per cycle): show both + delta.
    ipc_m = ratio(get(master, "instructions"), get(master, "cpu-cycles"))
    ipc_p = ratio(get(patch, "instructions"), get(patch, "cpu-cycles"))
    ipc_m_s = f"{ipc_m:.3f}" if ipc_m is not None else "n/a"
    ipc_p_s = f"{ipc_p:.3f}" if ipc_p is not None else "n/a"
    ipc_d_s = f"{ipc_p - ipc_m:+.3f}" if (ipc_m is not None and ipc_p is not None) else "n/a"
    print(f"{'IPC (insn/cycle)':<{label_w}} {ipc_m_s:>22} {ipc_p_s:>22} {ipc_d_s:>10}")

    # --- Reconstructed rates (delta in percentage points) ---
    print()
    rate_w = 24
    print(f"{'RATE':<{rate_w}} {'MASTER':>9} {'PATCH':>9} {'DELTA':>10}")
    print("-" * (rate_w + 1 + 9 + 1 + 9 + 1 + 10))

    itlb_den = (
        "bp_l1_tlb_fetch_hit",
        "bp_l1_tlb_miss_l2_tlb_hit",
        "bp_l1_tlb_miss_l2_tlb_miss",
    )

    def itlb_rate(d, num_name):
        """iTLB rate with the (L1-hit + L2-hit + full-miss) denominator."""
        num = get(d, num_name)
        parts = [get(d, n) for n in itlb_den]
        if num is None or any(p is None for p in parts):
            return None
        den = sum(parts)
        if den == 0:
            return None
        return 100.0 * num / den

    # (label, master_rate, patch_rate) tuples.
    rows = [
        ("branch-mispredict %",
         rate(master, "ex_ret_brn_misp", "ex_ret_brn"),
         rate(patch, "ex_ret_brn_misp", "ex_ret_brn")),
        ("icache hit %",
         rate(master, "ic_tag_hit_miss.instruction_cache_hit",
              "ic_tag_hit_miss.all_instruction_cache_accesses"),
         rate(patch, "ic_tag_hit_miss.instruction_cache_hit",
              "ic_tag_hit_miss.all_instruction_cache_accesses")),
        ("iTLB L1-hit %",
         itlb_rate(master, "bp_l1_tlb_fetch_hit"),
         itlb_rate(patch, "bp_l1_tlb_fetch_hit")),
        ("iTLB full-miss %",
         itlb_rate(master, "bp_l1_tlb_miss_l2_tlb_miss"),
         itlb_rate(patch, "bp_l1_tlb_miss_l2_tlb_miss")),
        ("L1-dcache miss % (approx)",
         rate(master, "L1-dcache-load-misses", "ls_dc_accesses"),
         rate(patch, "L1-dcache-load-misses", "ls_dc_accesses")),
        ("L1-DTLB miss % (approx)",
         rate(master, "ls_l1_d_tlb_miss.all", "ls_dc_accesses"),
         rate(patch, "ls_l1_d_tlb_miss.all", "ls_dc_accesses")),
    ]
    for label, mv, pv in rows:
        print(f"{label:<{rate_w}} {fmt_pct(mv):>9} {fmt_pct(pv):>9} {fmt_delta(mv, pv):>10}")

    print()
    print("Note: icache/iTLB/dTLB denominators are reconstructed from the event")
    print("group, and the dcache/dtlb rates are approximate -- their numerator")
    print("(loads only) and denominator (all accesses) measure slightly different")
    print("populations, so treat the absolute % as indicative, the delta as the signal.")
    print("Counts cover the backend thread only (perf -p pid); io_method=worker I/O")
    print("workers are not attributed (symmetric across master/patch).")
    print("\n" + "="*100 + "\n")


def prepare_prefetch_cache(conn, query_def, cached_mode):
    """Prepare cache state for a prefetch benchmark query."""
    if cached_mode:
        # Cached mode: prewarm everything (indexes + tables)
        print("Prewarming indexes and tables (cached mode)...")
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []), include_vm=True)
    else:
        # Uncached mode: evict heap, prewarm only indexes, clear OS cache
        print("Evicting relations and clearing OS cache (uncached mode)...")
        evict_relations(conn, query_def.get("evict", []))
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
        clear_os_cache()


def run_prefetch_query_profiling(args, master_bin, patch_bin, tmpfs_mount, perf_event, run_perf, prefetch_setting):
    """Profile a prefetch benchmark query and generate flame graphs."""
    query_id = args.queries.upper()
    query_def = ALL_QUERIES[query_id]
    sql_query = query_def["sql"].strip()
    cached_mode = args.cached
    verify_fn, load_fn = _get_data_functions(query_id)

    print(f"\n{'='*60}")
    print(f"Profiling prefetch query: {query_id} - {query_def['name']}")
    print(f"Mode: {'cached' if cached_mode else 'uncached'}")
    print(f"{'='*60}\n")

    # Create output directory
    if os.path.exists(OUTPUT_DIR):
        print(f"Removing previous output directory: {OUTPUT_DIR}")
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    stacks_file_master = os.path.join(OUTPUT_DIR, "master.stacks")
    stacks_file_patch = os.path.join(OUTPUT_DIR, "patch.stacks")
    folded_file_master = os.path.join(OUTPUT_DIR, "master.folded")
    folded_file_patch = os.path.join(OUTPUT_DIR, "patch.folded")
    svg_file_diff = os.path.join(OUTPUT_DIR, "diff.svg")
    svg_file_master = os.path.join(OUTPUT_DIR, "master_flamegraph.svg")
    svg_file_patch = os.path.join(OUTPUT_DIR, "patch_flamegraph.svg")

    try:
        # Verify/load data on both servers if needed
        if not args.skip_data_load:
            print("\n--- Verifying benchmark data on master ---")
            start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
            if not verify_fn(MASTER_CONN_DETAILS):
                print("Loading data on master (this will take several minutes)...")
                load_fn(MASTER_CONN_DETAILS)
            stop_server(master_bin, MASTER_DATA_DIR)
            time.sleep(2)

            print("\n--- Verifying benchmark data on patch ---")
            start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
            if not verify_fn(PATCH_CONN_DETAILS):
                print("Loading data on patch (this will take several minutes)...")
                load_fn(PATCH_CONN_DETAILS)
            stop_server(patch_bin, PATCH_DATA_DIR)
            time.sleep(2)

        # Profile master
        print("\n--- Profiling master version ---")
        start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
        perf_command_master, total_time_master = profile_prefetch_query(
            master_bin, "master", MASTER_CONN_DETAILS,
            stacks_file_master, sql_query, query_def, cached_mode,
            is_master=True, prefetch_setting=prefetch_setting,
            run_perf=run_perf, perf_event=perf_event,
            highfreq=args.highfreq, run_perfstat=args.perfstat,
            benchmark_cpu=args.benchmark_cpu, perf_cpu=args.perf_cpu,
            disable_prefetch=args.disable_prefetch,
            no_pin=args.no_pin,
        )
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(2)

        # Profile patch
        print("\n--- Profiling patch version ---")
        start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
        perf_command_patch, total_time_patch = profile_prefetch_query(
            patch_bin, "patch", PATCH_CONN_DETAILS,
            stacks_file_patch, sql_query, query_def, cached_mode,
            is_master=False, prefetch_setting=prefetch_setting,
            run_perf=run_perf, perf_event=perf_event,
            highfreq=args.highfreq, run_perfstat=args.perfstat,
            benchmark_cpu=args.benchmark_cpu, perf_cpu=args.perf_cpu,
            disable_prefetch=args.disable_prefetch,
            no_pin=args.no_pin,
        )
        stop_server(patch_bin, PATCH_DATA_DIR)

    finally:
        # Always stop both servers
        print("\n--- Stopping PostgreSQL servers ---")
        stop_server(master_bin, MASTER_DATA_DIR)
        stop_server(patch_bin, PATCH_DATA_DIR)

        # Cleanup tmpfs if it was created
        if tmpfs_mount:
            cleanup_tmpfs(tmpfs_mount)

    print(f"\nPatch min: {total_time_patch:.2f} ms, Master min: {total_time_master:.2f} ms")
    print(f"Patch query took \033[1m{total_time_patch/total_time_master:.3f}x\033[0m as long as master")

    if not run_perf and not args.perfstat:
        print("Perf profiling was disabled. Exiting.")
        return
    elif args.perfstat:
        # Compare the two per-event JSON files written by run_perfstat_passes.
        print("\n--- Perf Stat Output Comparison ---")
        display_perfstat_comparison(
            os.path.join(OUTPUT_DIR, "master_perfstat.json"),
            os.path.join(OUTPUT_DIR, "patch_perfstat.json"),
        )
        return

    # Generate flame graphs
    print("\n--- Generating flame graphs ---")
    generate_flamegraphs(
        stacks_file_master, stacks_file_patch,
        folded_file_master, folded_file_patch,
        svg_file_master, svg_file_patch, svg_file_diff,
        sql_query, perf_command_master, perf_command_patch
    )

    # Function-level master-vs-patch diff -- run it instead of just printing the
    # recipe.  Record several events (e.g. --perf cycles,instructions,branch-misses)
    # to get one diff block per event.
    print("\n--- perf diff (master vs patch, per event, function level) ---")
    write_perf_diff(os.path.join(OUTPUT_DIR, "master"),
                    os.path.join(OUTPUT_DIR, "patch"),
                    os.path.join(OUTPUT_DIR, "perf_diff.txt"))
    print(f"(saved to {OUTPUT_DIR}/perf_diff.txt)")

    # Self/children drill-down is interactive -- point at perf's own tooling
    # rather than freezing a static text report nobody re-reads.
    print("\n--- Drill into one profile ---")
    print(f"  perf report -i {OUTPUT_DIR}/master   # or {OUTPUT_DIR}/patch")
    print("  default --children = cumulative (dispersed cost); "
          "--no-children = self time (single hot spots)")

    # Display all generated flame graphs
    print("\n--- Displaying flame graphs with imgcat ---")
    for svg_file in [svg_file_master, svg_file_patch, svg_file_diff]:
        if os.path.exists(svg_file):
            print(f"Displaying {os.path.basename(svg_file)}...")
            try:
                subprocess.run(["imgcat", svg_file], check=False)
            except FileNotFoundError:
                print("(imgcat not available for inline display)")
                break


def profile_prefetch_query(pg_bin_dir, pg_name, conn_details, output_file, sql_query,
                           query_def, cached_mode, is_master, prefetch_setting=None,
                           run_perf=False, perf_event=None,
                           highfreq=False, run_perfstat=False,
                           benchmark_cpu=14, perf_cpu=15, disable_prefetch=False,
                           no_pin=False):
    """Profile a prefetch benchmark query with appropriate cache preparation."""
    print(f"--- Testing {pg_name} ---")

    conn = None
    try:
        # Connect to the database
        conn = psycopg.connect(**conn_details, prepare_threshold=0)
        backend_pid = conn.info.backend_pid
        print(f"Successfully connected. Backend PID is: {backend_pid}")

        # Pin the backend process (shared with prefetch_benchmark; off unless --pin)
        if not no_pin:
            pin_backend(backend_pid, benchmark_cpu, enabled=True)
        else:
            print("CPU pinning disabled (default; --pin to enable)")

        # Disable hardware prefetchers if requested
        original_prefetch_value = None
        if disable_prefetch:
            print(f"\nDisabling hardware prefetchers on CPU {benchmark_cpu}...")
            original_prefetch_value = disable_prefetchers(benchmark_cpu)

        # Prepare cache state
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm;")
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache;")

        prepare_prefetch_cache(conn, query_def, cached_mode)

        # Set GUCs for the query
        gucs = query_def.get("gucs", {})
        set_gucs(conn, gucs, is_master=is_master, prefetch_setting=prefetch_setting)

        # Prepare the query once to avoid planning overhead in timing.
        # force_generic_plan ensures EXECUTE reuses the cached plan
        # without per-execution plan cache overhead.
        stmt_name = "_bench_stmt"
        with conn.cursor() as cursor:
            cursor.execute("SET plan_cache_mode = force_generic_plan")
            cursor.execute(f"PREPARE {stmt_name} AS {sql_query}")

        # Handle warmup queries (A3 special case)
        if query_def.get("warmup_query") and not cached_mode:
            print("Running warmup queries...")
            with conn.cursor() as cursor:
                cursor.execute(f"EXECUTE {stmt_name}")
                cursor.execute(f"EXECUTE {stmt_name}")

        # Show the query text and the plan only (plain EXPLAIN -- no ANALYZE, so
        # the query is not executed or instrumented just to print its shape).
        print("\n--- Query Plan (EXPLAIN) ---")
        print(sql_query)
        print()
        with conn.cursor() as cursor:
            cursor.execute(f"EXPLAIN {sql_query}")
            for row in cursor.fetchall():
                print(row[0])
        print("--- End Query Plan ---\n")

        # Canonical no-instrumentation timing: the same EXECUTE-and-discard path
        # the default prefetch_benchmark uses (time_discard).  It EXECUTEs the
        # prepared statement (_bench_stmt, prepared above under force_generic_plan)
        # and discards the rows -- forming per-row output (approximating SERIALIZE)
        # without re-planning the query and without the EXPLAIN ANALYZE per-node
        # wrapper that inflates index-bound scans.  Cache prep already happened
        # above (prepare_prefetch_cache); it is NOT re-run inside the measured loop.
        sql = sql_query
        def run_one():
            return time_discard(conn, sql, serialize=True)

        perf_process = None
        perf_command = []
        query_time_ms = None

        if run_perfstat:
            # Curated AMD Zen 3 event passes over a constant workload.  This is
            # both the measurement and (via run_one) the timing source.
            stat_json = OUTPUT_DIR + "/" + pg_name + "_perfstat.json"
            print(f"Running perf-stat passes ({len(PERFSTAT_PASSES)} passes x "
                  f"{PERFSTAT_REPS} reps)...")
            run_perfstat_passes(conn, run_one, backend_pid, stat_json, perf_cpu)
            # Also grab perf's own default `perf stat` report (the familiar view).
            run_perf_stat_native(run_one, backend_pid,
                                 OUTPUT_DIR + "/" + pg_name + "_perfstat_native.txt",
                                 perf_cpu)
            # A quick separate timing for the comparison summary.
            query_time_ms = min(run_one() for _ in range(3))
            print(f"  Min query time: {query_time_ms:.2f} ms")

        elif run_perf:
            # perf record wants the whole core (kernel + io workers) for the
            # flame graph, so keep -C benchmark_cpu (or -p in --no-pin mode).
            perf_freq = "49999" if highfreq else str(PERF_FREQUENCY)
            perf_command = ["perf", "record", "-F", perf_freq]
            if perf_event:
                perf_command.extend(["-e", perf_event])
            if no_pin:
                print(f"Starting perf on PID {backend_pid}...")
                perf_command.extend(["-p", str(backend_pid), "-g", "-o", OUTPUT_DIR + "/" + pg_name])
            else:
                print(f"Starting perf on CPU core {benchmark_cpu}...")
                perf_command.extend(["-C", str(benchmark_cpu), "-g", "-o", OUTPUT_DIR + "/" + pg_name])
            perf_process = subprocess.Popen(perf_command)

            if not no_pin:
                try:
                    os.sched_setaffinity(perf_process.pid, {perf_cpu})
                    print(f"Pinned perf PID {perf_process.pid} to CPU {perf_cpu}.")
                except (AttributeError, PermissionError, OSError) as e:
                    print(f"Warning: Could not set CPU affinity for perf: {e}")

            time.sleep(1)  # perf record needs time to attach before the workload

            # perf record validates -e lazily and exits immediately on a bad event
            # or permission problem; catch that here instead of SIGINT-ing a dead
            # process and emitting an empty stack trace / flame graph.
            if perf_process.poll() is not None:
                raise RuntimeError(
                    f"perf record exited immediately (rc={perf_process.returncode}); "
                    f"command: {' '.join(perf_command)}")

            # Run the query a fixed number of times so the recorded window is
            # the query itself, not idle/setup.
            perf_record_reps = 10
            print(f"Executing query {perf_record_reps} times for profiling...")
            times = []
            for i in range(perf_record_reps):
                t = run_one()
                times.append(t)
                print(f"  Run {i+1}: {t:.2f} ms")
            query_time_ms = min(times)
            print(f"  Min: {query_time_ms:.2f} ms")

            print("Stopping perf...")
            perf_process.send_signal(signal.SIGINT)
            perf_process.wait(timeout=10)

            print("Generating stack trace file...")
            perf_data_file = OUTPUT_DIR + "/" + pg_name
            perf_script_cmd = ["perf", "script", "-i", perf_data_file]
            with open(output_file, "w") as f:
                subprocess.run(perf_script_cmd, stdout=f, check=True)

        else:
            # No perf: just time the query.
            times = [run_one() for _ in range(5)]
            query_time_ms = min(times)
            print(f"  Min query time: {query_time_ms:.2f} ms")

        # Restore hardware prefetchers if they were disabled
        if original_prefetch_value is not None:
            restore_prefetchers(benchmark_cpu, original_prefetch_value)

        # Deallocate prepared statement
        with conn.cursor() as cursor:
            cursor.execute(f"DEALLOCATE {stmt_name}")

        # Reset GUCs
        if gucs:
            reset_gucs(conn, gucs)

        # Return min execution time (in ms) for accurate comparison
        return perf_command, query_time_ms

    finally:
        if conn:
            conn.close()


def generate_flamegraphs(stacks_file_master, stacks_file_patch,
                         folded_file_master, folded_file_patch,
                         svg_file_master, svg_file_patch, svg_file_diff,
                         sql_query, perf_command_master, perf_command_patch):
    """Generate individual and differential flame graphs."""
    STACKCOLLAPSE = os.path.join(FLAMEGRAPH_DIR, "stackcollapse-perf.pl")
    FLAMEGRAPH = os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl")
    DIFFFOLDED = os.path.join(FLAMEGRAPH_DIR, "difffolded.pl")

    # Fold master stacks
    print("Folding master stacks...")
    with open(folded_file_master, "w") as f:
        subprocess.run([STACKCOLLAPSE, stacks_file_master], stdout=f, check=True)

    # Fold patch stacks
    print("Folding patch stacks...")
    with open(folded_file_patch, "w") as f:
        subprocess.run([STACKCOLLAPSE, stacks_file_patch], stdout=f, check=True)

    # Generate master flame graph
    print("Generating master flame graph...")
    with open(svg_file_master, "w") as f:
        perf_cmd_str = " ".join(perf_command_master) if perf_command_master else "perf record"
        subprocess.run([
            FLAMEGRAPH,
            "--title", f'master, "{sql_query[:50]}..."',
            "--subtitle", perf_cmd_str,
            folded_file_master
        ], stdout=f, check=True)

    # Generate patch flame graph
    print("Generating patch flame graph...")
    with open(svg_file_patch, "w") as f:
        perf_cmd_str = " ".join(perf_command_patch) if perf_command_patch else "perf record"
        subprocess.run([
            FLAMEGRAPH,
            "--title", f'patch, "{sql_query[:50]}..."',
            "--subtitle", perf_cmd_str,
            folded_file_patch
        ], stdout=f, check=True)

    # Generate differential flame graph
    print("Generating differential flame graph...")
    diff_proc = subprocess.run(
        [DIFFFOLDED, folded_file_master, folded_file_patch],
        capture_output=True, check=True
    )
    with open(svg_file_diff, "w") as f:
        subprocess.run([
            FLAMEGRAPH,
            "--title", f'master versus patch, "{sql_query[:50]}..."',
            "--negate"
        ], input=diff_proc.stdout, stdout=f, check=True)

    print(f"\nFlame graphs generated:")
    print(f"  Master: {svg_file_master}")
    print(f"  Patch: {svg_file_patch}")
    print(f"  Diff: {svg_file_diff}")


def main():
    """Main execution flow."""
    args = parse_arguments()

    # Fill io GUC knobs left unset from the per-io-method defaults, exactly as
    # prefetch_benchmark does, so start_server() applies identical settings.
    args = resolve_io_method_defaults(args)
    # Pinning is off by default (matches prefetch_benchmark); --pin opts in.
    args.no_pin = args.no_pin or not args.pin

    # Validate that --perf and --perfstat are mutually exclusive
    if args.perf and args.perfstat:
        print("Error: --perf and --perfstat cannot be used together")
        sys.exit(1)

    # Extract perf event from args.perf if it's a string, otherwise set to None
    perf_event = args.perf if isinstance(args.perf, str) else None
    run_perf = bool(args.perf)  # True if --perf was specified (with or without event)

    # Check perf_event_paranoid setting when using perf
    if run_perf or args.perfstat:
        try:
            with open("/proc/sys/kernel/perf_event_paranoid", "r") as f:
                paranoid = int(f.read().strip())
                if paranoid != -1:
                    print(f"Error: /proc/sys/kernel/perf_event_paranoid is {paranoid}, must be -1")
                    print("Fix with: sudo sysctl kernel.perf_event_paranoid=-1")
                    sys.exit(1)
        except (FileNotFoundError, ValueError) as e:
            print(f"Warning: Could not check perf_event_paranoid: {e}")

    # Fail fast on a bad --perf event (e.g. the invalid `--perf icache`) before
    # the whole server-start + workload run, rather than mid-flight in perf record.
    if run_perf and perf_event:
        validate_perf_events(perf_event)

    # Validate that --num-queries is only used with parameterized benchmarks
    if args.num_queries is not None and args.benchmark not in ("simple_select", "bitmap"):
        print("Error: --num-queries can only be used with --benchmark simple_select or bitmap")
        sys.exit(1)

    # Validate that --hash and --ios are not used together
    if args.use_hash_index and args.index_only_scan:
        print("Error: --hash and --ios cannot be used together (hash indexes lack support for index-only scans)")
        sys.exit(1)

    # Validate --queries and --cached arguments
    if args.queries and args.benchmark != "nestloop":  # nestloop is the default
        print("Error: --queries and --benchmark are mutually exclusive")
        sys.exit(1)

    if args.cached and not args.queries:
        print("Error: --cached requires --queries")
        sys.exit(1)


    if args.queries:
        query_id = args.queries.upper()
        if query_id not in ALL_QUERIES:
            available = ", ".join(ALL_QUERIES.keys())
            print(f"Error: Unknown query '{args.queries}'. Available: {available}")
            sys.exit(1)

    # Stop background processes that would skew results.
    # Interactive: prompts (answer n to keep them running while debugging);
    # non-interactive: --force.  perf_flamegraph runs its own benchmark loop (it
    # only imports data/helpers from prefetch_benchmark, it does not delegate
    # execution to it), so it needs this independently.
    kill_interfering_processes()

    # Verify THP / CPU governor are benchmark-friendly (warns + prompts, aborts
    # if declined).  Needed independently here for the same reason as the kill.
    check_benchmark_env()

    # Pin the script itself to a core to avoid it interfering with the benchmark.
    if not args.no_pin:
        try:
            pid = os.getpid()
            os.sched_setaffinity(pid, {0})
            print(f"Pinned this script (PID {pid}) to CPU 0.")
        except (AttributeError, PermissionError, OSError) as e:
            print(f"Warning: Could not set CPU affinity for this script: {e}")

    check_dependencies()

    # Resolve baseline and testbranch configuration
    global MASTER_CONN_DETAILS, MASTER_DATA_DIR, PATCH_CONN_DETAILS, PATCH_DATA_DIR
    baseline_name = args.baseline
    baseline_bin_orig, baseline_data_dir, _baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    MASTER_CONN_DETAILS = baseline_conn
    MASTER_DATA_DIR = baseline_data_dir

    testbranch_name = args.testbranch
    test_bin_orig, test_data_dir, _test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    PATCH_CONN_DETAILS = test_conn
    PATCH_DATA_DIR = test_data_dir

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    original_master_bin = baseline_bin_orig
    original_patch_bin = test_bin_orig
    master_bin = baseline_bin_orig
    patch_bin = test_bin_orig

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(baseline_bin_orig, tmpfs_mount, "baseline")
        patch_bin = copy_binaries_to_tmpfs(test_bin_orig, tmpfs_mount, "testbranch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  baseline ({baseline_name}): {master_bin}")
        print(f"  testbranch ({testbranch_name}): {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")
        master_bin = baseline_bin_orig
        patch_bin = test_bin_orig

    # Determine prefetch setting for patch/test version
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]
    test_is_master = not test_has_prefetch
    if test_has_prefetch:
        if args.prefetch_only:
            prefetch_setting = "on"
        elif args.prefetch_disabled:
            prefetch_setting = "off"
        else:
            prefetch_setting = None
    else:
        # Testbranch has no prefetch GUC — don't try to set it
        prefetch_setting = None

    # Handle prefetch benchmark query mode
    if args.queries:
        run_prefetch_query_profiling(
            args, master_bin, patch_bin, tmpfs_mount,
            perf_event, run_perf, prefetch_setting
        )
        return

    # Get benchmark configuration
    benchmark = BENCHMARKS[args.benchmark]
    sql_query = benchmark["sql_query"]
    query_repetitions = benchmark["query_repetitions"]
    max_aid_val = benchmark.get("max_aid_val")
    use_bitmap = args.benchmark == "bitmap"
    bitmap_range = args.bitmap_range if use_bitmap else benchmark.get("bitmap_range", 2000)

    # Override query_repetitions if --queries was provided
    if args.num_queries is not None:
        query_repetitions = args.num_queries

    # Modify SQL query for index-only scan if --ios is used
    if args.index_only_scan:
        if args.benchmark == "simple_select":
            # Change "select *" to "select aid" to enable index-only scans
            sql_query = sql_query.replace("select *", "select aid")

    if os.path.exists(OUTPUT_DIR):
        print(f"Removing previous output directory: {OUTPUT_DIR}")
        shutil.rmtree(OUTPUT_DIR)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"--- Profiling \"{sql_query}\" ---")
    print(f"Using benchmark: {args.benchmark}")

    stacks_file_master = os.path.join(OUTPUT_DIR, "master.stacks")
    stacks_file_patch = os.path.join(OUTPUT_DIR, "patch.stacks")
    folded_file_master = os.path.join(OUTPUT_DIR, "master.folded")
    folded_file_patch = os.path.join(OUTPUT_DIR, "patch.folded")
    svg_file_diff = os.path.join(OUTPUT_DIR, "diff.svg")
    svg_file_master = os.path.join(OUTPUT_DIR, "master_flamegraph.svg")
    svg_file_patch = os.path.join(OUTPUT_DIR, "patch_flamegraph.svg")

    try:
        # Initialize pgbench if requested (must do one at a time due to shared memory constraints)
        if not args.skip_pgbench_init:
            print("--- Initializing pgbench for master ---")
            start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
            init_pgbench(master_bin, MASTER_CONN_DETAILS, args.pgbench_scale, args.use_hash_index)
            stop_server(master_bin, MASTER_DATA_DIR)
            time.sleep(2)

            print("--- Initializing pgbench for patch ---")
            start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
            init_pgbench(patch_bin, PATCH_CONN_DETAILS, args.pgbench_scale, args.use_hash_index)
            stop_server(patch_bin, PATCH_DATA_DIR)
            time.sleep(2)
        else:
            print("Skipping pgbench initialization.")
            # Verify that existing indexes match the requested type
            if args.use_hash_index:
                print("\n--- Verifying existing hash indexes ---")
                start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
                verify_hash_index_exists("master", MASTER_CONN_DETAILS)
                stop_server(master_bin, MASTER_DATA_DIR)
                time.sleep(1)

                start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
                verify_hash_index_exists("patch", PATCH_CONN_DETAILS)
                stop_server(patch_bin, PATCH_DATA_DIR)
                time.sleep(1)
            else:
                print("\n--- Verifying existing B-tree indexes ---")
                start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
                verify_btree_index_exists("master", MASTER_CONN_DETAILS)
                stop_server(master_bin, MASTER_DATA_DIR)
                time.sleep(1)

                start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
                verify_btree_index_exists("patch", PATCH_CONN_DETAILS)
                stop_server(patch_bin, PATCH_DATA_DIR)
                time.sleep(1)

        # Get row counts from both servers and verify they match
        print("\n--- Verifying pgbench_accounts row counts ---")
        start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
        master_row_count = get_pgbench_row_count("master", MASTER_CONN_DETAILS)
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(1)

        start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
        patch_row_count = get_pgbench_row_count("patch", PATCH_CONN_DETAILS)
        stop_server(patch_bin, PATCH_DATA_DIR)
        time.sleep(1)

        if master_row_count != patch_row_count:
            print(f"Error: Row count mismatch! master={master_row_count}, patch={patch_row_count}")
            sys.exit(1)

        # Use the actual row count for parameterized benchmarks
        if args.benchmark == "simple_select":
            max_aid_val = master_row_count
            print(f"Using max_aid_val={max_aid_val} for simple_select benchmark")
        elif args.benchmark == "bitmap":
            max_aid_val = master_row_count
            print(f"Using max_aid_val={max_aid_val}, bitmap_range={bitmap_range} for bitmap benchmark")

        # Profile in the order specified by args.patch_first
        if args.patch_first:
            # Profile patch first
            print("--- Profiling patch version ---")
            start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
            perf_command_patch, total_time_patch = profile_postgres(
                patch_bin, "patch", PATCH_CONN_DETAILS,
                stacks_file_patch, sql_query, query_repetitions, run_perf, max_aid_val, perf_event, args.highfreq, args.index_only_scan, args.perfstat, args.discard_runs, args.benchmark_cpu, args.perf_cpu, args.disable_prefetch, is_master=test_is_master, prefetch_setting=prefetch_setting, no_pin=args.no_pin, use_bitmap=use_bitmap, bitmap_range=bitmap_range)
            stop_server(patch_bin, PATCH_DATA_DIR)
            time.sleep(2)

            # Then profile master
            print("--- Profiling master version ---")
            start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
            perf_command_master, total_time_master = profile_postgres(
                master_bin, "master", MASTER_CONN_DETAILS,
                stacks_file_master, sql_query, query_repetitions, run_perf, max_aid_val, perf_event, args.highfreq, args.index_only_scan, args.perfstat, args.discard_runs, args.benchmark_cpu, args.perf_cpu, args.disable_prefetch, is_master=True, prefetch_setting=prefetch_setting, no_pin=args.no_pin, use_bitmap=use_bitmap, bitmap_range=bitmap_range)
            stop_server(master_bin, MASTER_DATA_DIR)
        else:
            # Profile master first (default)
            print("--- Profiling master version ---")
            start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, args, on_patch=False)
            perf_command_master, total_time_master = profile_postgres(
                master_bin, "master", MASTER_CONN_DETAILS,
                stacks_file_master, sql_query, query_repetitions, run_perf, max_aid_val, perf_event, args.highfreq, args.index_only_scan, args.perfstat, args.discard_runs, args.benchmark_cpu, args.perf_cpu, args.disable_prefetch, is_master=True, prefetch_setting=prefetch_setting, no_pin=args.no_pin, use_bitmap=use_bitmap, bitmap_range=bitmap_range)
            stop_server(master_bin, MASTER_DATA_DIR)
            time.sleep(2)

            # Then profile patch
            print("--- Profiling patch version ---")
            start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, args, on_patch=True)
            perf_command_patch, total_time_patch = profile_postgres(
                patch_bin, "patch", PATCH_CONN_DETAILS,
                stacks_file_patch, sql_query, query_repetitions, run_perf, max_aid_val, perf_event, args.highfreq, args.index_only_scan, args.perfstat, args.discard_runs, args.benchmark_cpu, args.perf_cpu, args.disable_prefetch, is_master=test_is_master, prefetch_setting=prefetch_setting, no_pin=args.no_pin, use_bitmap=use_bitmap, bitmap_range=bitmap_range)
            stop_server(patch_bin, PATCH_DATA_DIR)

    finally:
        # Always stop both servers
        print("--- Stopping PostgreSQL servers ---")
        stop_server(master_bin, MASTER_DATA_DIR)
        stop_server(patch_bin, PATCH_DATA_DIR)

        # Cleanup tmpfs if it was created
        if tmpfs_mount:
            cleanup_tmpfs(tmpfs_mount)

    print(f"Patch query loop took \033[1m{total_time_patch/total_time_master:.3f}x\033[0m as long as master")

    if not run_perf and not args.perfstat:
        print("Perf profiling was disabled. Exiting.")
        return
    elif args.perfstat:
        # Compare the two per-event JSON files written by run_perfstat_passes.
        display_perfstat_comparison(
            os.path.join(OUTPUT_DIR, "master_perfstat.json"),
            os.path.join(OUTPUT_DIR, "patch_perfstat.json"),
        )
        print("perf stat mode complete. Skipping flamegraph generation.")
        return

    # --- Generate Flame Graphs ---
    print("--- Generating Flame Graphs ---")

    # Fold stack traces
    print("Folding stack traces...")
    with open(folded_file_master, "w") as f1, open(folded_file_patch, "w") as f2:
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "stackcollapse-perf.pl"), stacks_file_master],
            stdout=f1, check=True
        )
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "stackcollapse-perf.pl"), stacks_file_patch],
            stdout=f2, check=True
        )

    print("Creating individual flame graph for master...")
    with open(svg_file_master, "w") as f_svg:
        subprocess.run(
                [
                    os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl"),
                    "--title", "master, \"" + sql_query + "\"",
                    "--subtitle", perf_command_master,
                    folded_file_master,
                    ],
                stdout=f_svg,
                check=True,
                )
    print(f"master flame graph created: {svg_file_master}")

    print("Creating individual flame graph for patch...")
    with open(svg_file_patch, "w") as f_svg:
        subprocess.run(
                [
                    os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl"),
                    "--title", "patch, \"" + sql_query + "\"",
                    "--subtitle", perf_command_patch,
                    folded_file_patch,
                    ],
                stdout=f_svg,
                check=True,
                )
    print(f"patch flame graph created: {svg_file_patch}")

    # Create the differential SVG
    print("Creating the differential flame graph...")
    difffolded_cmd = [os.path.join(FLAMEGRAPH_DIR, "difffolded.pl"), folded_file_master, folded_file_patch]

    flamegraph_cmd = [
        os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl"),
        "--title", "master versus patch, \"" + sql_query + "\"",
        "--subtitle", perf_command_master + ", " + perf_command_patch,
    ]

    p1 = subprocess.Popen(difffolded_cmd, stdout=subprocess.PIPE)
    with open(svg_file_diff, "w") as f_svg:
        subprocess.run(flamegraph_cmd, stdin=p1.stdout, stdout=f_svg, check=True)
    p1.stdout.close()

    print(f"Differential flame graph created: {svg_file_diff}")

    # Function-level master-vs-patch diff -- run it instead of just printing the
    # recipe.  Record several events (e.g. --perf cycles,instructions,branch-misses)
    # to get one diff block per event.
    print("\n--- perf diff (master vs patch, per event, function level) ---")
    write_perf_diff(os.path.join(OUTPUT_DIR, "master"),
                    os.path.join(OUTPUT_DIR, "patch"),
                    os.path.join(OUTPUT_DIR, "perf_diff.txt"))
    print(f"(saved to {OUTPUT_DIR}/perf_diff.txt)")

    # Self/children drill-down is interactive -- point at perf's own tooling
    # rather than freezing a static text report nobody re-reads.
    print("\n--- Drill into one profile ---")
    print(f"  perf report -i {OUTPUT_DIR}/master   # or {OUTPUT_DIR}/patch")
    print("  default --children = cumulative (dispersed cost); "
          "--no-children = self time (single hot spots)")

    # Display all generated SVG files using imgcat
    print("\n--- Displaying flame graphs with imgcat ---")
    svg_files = [svg_file_master, svg_file_patch, svg_file_diff]
    for svg_file in svg_files:
        if os.path.exists(svg_file):
            print(f"Displaying {os.path.basename(svg_file)}...")
            subprocess.run(["imgcat", svg_file], check=False)
        else:
            print(f"Warning: SVG file not found: {svg_file}")

    print("Done.")

if __name__ == "__main__":
    main()
