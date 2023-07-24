#!/usr/bin/env python3
"""
Prefetch benchmark script for PostgreSQL index scan prefetching.

Compares query performance across three configurations:
1. master (no prefetching, GUC not available)
2. patch with debug_disable_indexscan_prefetch=on (prefetching off)
3. patch with debug_disable_indexscan_prefetch=off (prefetching on)

Usage:
    ./prefetch_benchmark.py                    # Run all queries, uncached, 3 runs
    ./prefetch_benchmark.py --cached           # Run with data prewarmed
    ./prefetch_benchmark.py --queries Q1,Q2    # Run specific queries
    ./prefetch_benchmark.py --queries WR1,Q1,GG1  # Queries from several suites at once
    ./prefetch_benchmark.py --runs 5           # >=5 runs per query (per-query floor wins if higher)
    ./prefetch_benchmark.py --skip-load        # Skip data loading verification
    ./prefetch_benchmark.py --no-serialize     # Exclude serialization overhead from timing
"""

import argparse
import atexit
import glob
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from statistics import mean, median

import psycopg

# ── Shared configuration & benchmark infrastructure ───────────────────

# --- Configuration ---

# PREFETCH_MASTER_BIN / PREFETCH_PATCH_BIN override the baseline / testbranch
# bindir (scratch installs).
MASTER_BIN = os.environ.get("PREFETCH_MASTER_BIN",
                            "/mnt/nvme/postgresql/master/install_meson_rc/bin")
PATCH_BIN = os.environ.get("PREFETCH_PATCH_BIN",
                           "/mnt/nvme/postgresql/patch/install_meson_rc/bin")
PATCH_DATA_DIR = "/mnt/nvme/postgresql/patch/data"
# master runs against the patch build's data directory, not a copy of its own.
# Both are the same major version and, as long as the patch series carries no
# catalog change, the same catalog version, so master opens the patch cluster
# as-is.  Benchmarking both builds against the very same files also removes
# the physical-layout differences a separately loaded copy would introduce,
# leaves nothing to load or keep in sync (optimizer statistics included), and
# spares a second copy of the data.  A back branch cannot open this cluster at
# all -- a different major version -- so each keeps a dedicated data directory.
# preflight_data_dir() checks every server binary against its data directory
# before anything runs, so a master install left unrebuilt across a catversion
# bump fails at once with a clear message instead of partway through a run.
MASTER_DATA_DIR = PATCH_DATA_DIR
MASTER_SOURCE_DIR = "/mnt/nvme/postgresql/master/source"
PATCH_SOURCE_DIR = "/mnt/nvme/postgresql/patch/source"

REL18_BIN = "/mnt/nvme/postgresql/REL_18_STABLE/install_meson_rc/bin"
REL18_DATA_DIR = "/mnt/nvme/postgresql/REL_18_STABLE/data"
REL18_SOURCE_DIR = "/mnt/nvme/postgresql/REL_18_STABLE/source"

MASTER_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5555,
}
PATCH_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5432,
}
REL18_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5418,
}

# Map baseline name to (bin, data_dir, source_dir, conn) for easy lookup
BASELINE_CONFIGS = {
    "master": (MASTER_BIN, MASTER_DATA_DIR, MASTER_SOURCE_DIR, MASTER_CONN),
    "rel18":  (REL18_BIN, REL18_DATA_DIR, REL18_SOURCE_DIR, REL18_CONN),
}

# Map testbranch name to (bin, data_dir, source_dir, conn) for easy lookup
TESTBRANCH_CONFIGS = {
    "patch":  (PATCH_BIN, PATCH_DATA_DIR, PATCH_SOURCE_DIR, PATCH_CONN),
    "master": (MASTER_BIN, MASTER_DATA_DIR, MASTER_SOURCE_DIR, MASTER_CONN),
}

# Whether each build has the debug_disable_indexscan_prefetch GUC
BUILD_HAS_PREFETCH = {
    "patch": True,
    "master": False,
    "rel18": False,
}


# Set by verify_test_has_prefetch() when a testbranch build was expected to have
# the prefetch GUC but didn't.  Re-printed by _print_wall_time() at the very end
# of the run so the warning isn't lost in the scrollback of a long benchmark.
MISSING_PREFETCH_GUC_TESTBRANCH = None


def server_has_prefetch_guc(conn):
    """Return True if the connected server actually defines the
    debug_disable_indexscan_prefetch GUC.  BUILD_HAS_PREFETCH is the *static* declaration
    of what each bindir is expected to provide; this checks the running server."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_settings "
                    "WHERE name = 'debug_disable_indexscan_prefetch'")
        return cur.fetchone() is not None


def print_missing_prefetch_guc_warning(testbranch_name):
    """Print the prominent banner warning that the testbranch build lacks the
    debug_disable_indexscan_prefetch GUC.  Used both inline (at detection) and again at
    the very end of the run."""
    line = "!" * 72
    print(f"\n{line}")
    print(f"!! WARNING: testbranch '{testbranch_name}' has NO "
          f"debug_disable_indexscan_prefetch GUC,")
    print( "!! but BUILD_HAS_PREFETCH expects it.  Benchmarked this build like "
           "a baseline")
    print( "!! (no prefetch=off/on split).  Point the bindir at a patched build "
           "to compare prefetch.")
    print(f"{line}\n")


def verify_test_has_prefetch(conn, test_has_prefetch, testbranch_name):
    """Reconcile the static BUILD_HAS_PREFETCH expectation with the running
    testbranch server.

    If the build is expected to have debug_disable_indexscan_prefetch but the server
    doesn't (e.g. the bindir was rebuilt off a branch without the patch, or
    points at a stock master), print a prominent warning and downgrade to False
    so the build is benchmarked like a baseline -- no prefetch=off/on split --
    instead of erroring out later on SET debug_disable_indexscan_prefetch.  The warning
    is also re-printed at the very end of the run (see _print_wall_time)."""
    if not test_has_prefetch or server_has_prefetch_guc(conn):
        return test_has_prefetch
    global MISSING_PREFETCH_GUC_TESTBRANCH
    MISSING_PREFETCH_GUC_TESTBRANCH = testbranch_name
    print_missing_prefetch_guc_warning(testbranch_name)
    return False


# --- Result classification (shared by prefetch_benchmark.py + patch_report.py) ---
#
# A patch-vs-baseline per-query result is an improvement / regression / neutral.
# Crucially, a difference that is tiny in ABSOLUTE terms is noise -- classified
# neutral no matter how large the percentage -- so e.g. 0.133ms vs 0.134ms, or
# even 0.133ms vs 0.5ms (a 3.8x ratio, but pure timer noise on a sub-ms query),
# is not a regression.  Callers exclude neutral results from the geometric mean
# and best/worst, since those (often extreme) ratios would otherwise distort it.
RATIO_NEUTRAL_BAND = 0.01   # within +/- 1% ratio -> neutral
NOISE_ABS_MS = 1.0          # abs diff <= this, at ANY query size -> noise -> neutral


def classify_ratio(master_ms, patch_ms, noise_abs_ms=NOISE_ABS_MS):
    """Return 'improvement', 'regression', or 'neutral' for one per-query result.

    A difference within the absolute noise floor (noise_abs_ms, default
    NOISE_ABS_MS; overridable via prefetch_benchmark's --noise-abs-ms) is noise
    -> neutral regardless of the ratio or the query's execution time.  So a
    "regression" requires the patch to be slower both relatively (beyond
    RATIO_NEUTRAL_BAND) and absolutely (more than noise_abs_ms); a sub-floor
    delta is never a regression no matter how large the percentage.
    """
    if not master_ms or master_ms <= 0 or patch_ms is None:
        return "neutral"
    if abs(patch_ms - master_ms) <= noise_abs_ms:
        return "neutral"
    ratio = patch_ms / master_ms
    if ratio < 1.0 - RATIO_NEUTRAL_BAND:
        return "improvement"
    if ratio > 1.0 + RATIO_NEUTRAL_BAND:
        return "regression"
    return "neutral"


# --- Tmpfs Hugepages Functions ---

# Resolve the tmpfs/hugepages helper via $PATH (single source of truth, the same
# script run.sh uses).  Resolve to a full path because mount/umount run under
# sudo, which searches its own secure_path rather than our $PATH -- a bare name
# wouldn't be found there.  Fall back to the bare name if not found so import
# never fails; the error then surfaces at first use.
TMPFS_SCRIPT = shutil.which("pg_tmpfs_hugepages.sh") or "pg_tmpfs_hugepages.sh"


def setup_tmpfs_hugepages():
    """Mount a tmpfs with huge=always and return the mount point path."""
    result = subprocess.run(
        ["sudo", TMPFS_SCRIPT, "mount", "-s", "800M"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"Error: Failed to mount tmpfs: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


def copy_binaries_to_tmpfs(src_bin_dir, tmpfs_mount, version_name):
    """Copy PostgreSQL binaries to tmpfs and return the new path."""
    result = subprocess.run(
        [TMPFS_SCRIPT, "copy", src_bin_dir, version_name],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"Error: Failed to copy binaries: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


def cleanup_tmpfs(tmpfs_mount):
    """Unmount the tmpfs filesystem."""
    result = subprocess.run(
        ["sudo", TMPFS_SCRIPT, "umount"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"Warning: Failed to unmount tmpfs: {result.stderr.strip()}")
    elif result.stderr:
        print(result.stderr.strip())


# --- Background Process Interference ---

KILL_INTERFERERS_SCRIPT = "kill_vscode_server.sh"


def kill_interfering_processes():
    """Stop background processes that would skew benchmark measurements.

    Interactive (stdin is a tty): prompt -- you are shown what is running and
    answer y/n once, all-or-nothing.  Answer n to keep those other
    sessions alive while debugging; the benchmark then proceeds with the
    processes still running.  Non-interactive (e.g. cron): --force, killing
    without prompting.
    """
    cmd = [KILL_INTERFERERS_SCRIPT]
    if not sys.stdin.isatty():
        cmd.append("--force")
    result = subprocess.run(cmd, check=False)
    # Exit 2 = you declined at the prompt -> proceed with them running.
    # Exit 1 = --force hit clangd, or a confirmed kill failed -> refuse.
    if result.returncode == 1:
        print("Error: refusing to benchmark with interfering processes running "
              "(see message above). Stop them and retry.")
        sys.exit(1)


CHECK_ENV_SCRIPT = "check_benchmark_env.sh"


def check_benchmark_env():
    """Verify the machine is in a benchmark-friendly state (THP, CPU governor).
    """
    result = subprocess.run(
        [CHECK_ENV_SCRIPT],
        check=False,
    )
    if result.returncode != 0:
        print("Error: benchmark environment check failed or was declined "
              "(see message above). Fix it and retry.")
        sys.exit(1)


# --- NVMe Power State ---
#
# With default power management an idle NVMe drops into a non-operational
# power state after ~100 ms, and the next read pays a fixed wake-up latency of
# several ms.  An uncached run idles the device for at least that long before
# every timed query (eviction, prewarm, OS cache drop), so short queries
# measure the wake-up rather than the I/O: bimodal times, and a large apparent
# regression for whichever side caught the device asleep more often.  Setting
# each controller's PM QoS latency tolerance to 0 forbids the non-operational
# states; "sudo perf_stability.sh prepare --nvme-awake" does that (its
# "restore" puts the default back).  Every uncached run made with the default
# power management ends with a warning, whatever its numbers came out as.
NVME_PM_QOS_GLOB = "/sys/class/nvme/nvme*/power/pm_qos_latency_tolerance_us"
PERF_STABILITY_SCRIPT = "perf_stability.sh"

# Set by note_nvme_power_state(); printed by _print_wall_time() as the very
# last output of the run, like the missing-GUC warning.
NVME_ASLEEP_WARNING = None


def nvme_kept_awake():
    """True if every NVMe controller forbids non-operational power states
    (pm_qos_latency_tolerance_us reads 0), False if any allows them, None if
    there is no NVMe controller or the knob cannot be read."""
    paths = glob.glob(NVME_PM_QOS_GLOB)
    if not paths:
        return None
    for path in paths:
        try:
            with open(path) as f:
                value = f.read().strip()
        except OSError:
            return None
        if value != "0":
            return False
    return True


def print_nvme_asleep_warning(text):
    line = "!" * 72
    print(f"\n{line}")
    for text_line in text.splitlines():
        print(f"!! {text_line}")
    print(f"{line}\n")


def note_nvme_power_state(args, nvme_awake):
    """Arm the end-of-run warning when an uncached run measures with the NVMe
    allowed to sleep.  nvme_awake is nvme_kept_awake() sampled at the start of
    the run.  A cached run's timed queries never reach the device, and None
    (no NVMe, or its state unknown) leaves nothing to say."""
    global NVME_ASLEEP_WARNING
    if args.cached or nvme_awake is not False:
        return
    # Resolve to a full path for the message: sudo searches its own
    # secure_path, not our $PATH.
    script = shutil.which(PERF_STABILITY_SCRIPT)
    script = re.sub(r"^/+", "/", script) if script else PERF_STABILITY_SCRIPT
    NVME_ASLEEP_WARNING = (
        "WARNING: this uncached run used default NVMe power management\n"
        "(pm_qos_latency_tolerance_us is not 0).  An idle NVMe enters a\n"
        "low-power state within ~100 ms and the next read pays a fixed\n"
        "multi-ms wake-up; uncached runs idle the device that long before\n"
        "every timed query, so short queries measure the wake-up, not the\n"
        "I/O.  Treat every ratio above with suspicion.  Hold the device\n"
        "awake and re-run:\n"
        f"    sudo {script} prepare --nvme-awake")


# --- Cache Management Functions ---

def clear_os_cache():
    """Clear the OS page cache."""
    result = subprocess.run(
        ["sudo", "clear_cache.sh"],
        capture_output=True,
        check=False
    )
    if result.returncode != 0:
        print("Warning: Failed to clear OS cache")


def evict_relations(conn, relations):
    """Evict relations from PostgreSQL buffer cache."""
    max_retries = 3
    with conn.cursor() as cur:
        for rel in relations:
            for attempt in range(max_retries):
                try:
                    cur.execute(f"SELECT * FROM pg_buffercache_evict_relation('{rel}')")
                    row = cur.fetchone()
                    if row:
                        buffers_evicted, buffers_flushed, buffers_skipped = row
                        if buffers_skipped > 0:
                            if attempt < max_retries - 1:
                                time.sleep(0.1)
                                continue  # Retry
                            print(f"Warning: Failed to evict {buffers_skipped} buffers from {rel} "
                                  f"after {max_retries} attempts "
                                  f"(evicted={buffers_evicted}, flushed={buffers_flushed})")
                    break  # Success or no row returned
                except Exception as e:
                    conn.rollback()
                    print(f"Warning: Failed to evict {rel}: {e}")
                    break


def prewarm_relations(conn, relations, include_vm=False, vm_only=False):
    """Prewarm relations into PostgreSQL buffer cache.

    If include_vm is True, also prewarm the visibility map fork.
    If vm_only is True, prewarm ONLY the visibility map fork (not main).
    include_vm should be set for heap relations (tables) but NOT for indexes,
    which have no VM fork.
    """
    with conn.cursor() as cur:
        for rel in relations:
            try:
                if not vm_only:
                    cur.execute(f"SELECT pg_prewarm('{rel}')")
                if include_vm or vm_only:
                    cur.execute(f"SELECT pg_prewarm('{rel}', 'buffer', 'vm')")
            except Exception as e:
                conn.rollback()
                print(f"Warning: Failed to prewarm {rel}: {e}")


def disable_prefetch_guc_value(prefetch):
    """Map a prefetch on/off setting to the value to use for the
    debug_disable_indexscan_prefetch GUC, whose meaning is inverted (on means
    that prefetching is disabled)."""
    return "off" if str(prefetch).lower() in ("on", "true", "1") else "on"


def set_gucs(conn, gucs, is_master=False, prefetch_setting=None):
    """Set GUCs for query execution."""
    with conn.cursor() as cur:
        # Base GUCs for all configurations
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")
        # EXECUTE is a utility statement, so the server materializes the whole
        # result set in a tuplestore (PORTAL_UTIL_SELECT) before sending any of
        # it.  Keep that tuplestore in memory for every query: the largest
        # result sets (A43: 20.7M rows) spill to temp files at the default
        # 100MB, and the timing then measures the spill and its readback more
        # than the scan (A39: 2.03s spilling vs 1.65s in memory; 1GB suffices,
        # 2GB leaves margin).  Same value on baseline and patch.
        cur.execute("SET work_mem = '2GB'")

        # Set prefetch GUC only on patch (not master)
        if not is_master and prefetch_setting is not None:
            cur.execute("SET debug_disable_indexscan_prefetch = "
                        f"{disable_prefetch_guc_value(prefetch_setting)}")

        # Query-specific GUCs
        for guc, value in gucs.items():
            cur.execute(f"SET {guc} = {value}")


def reset_gucs(conn, gucs):
    """Reset query-specific GUCs to defaults."""
    with conn.cursor() as cur:
        for guc in gucs:
            cur.execute(f"RESET {guc}")


# Benchmark suites are defined declaratively in suites/*.toml and loaded by
# benchmark_suites.py, which owns the single source of truth.  BENCHMARK_SUITES is
# re-exported here so existing consumers (e.g. perf_flamegraph.py) keep importing it
# from prefetch_benchmark.
from benchmark_suites import (
    BENCHMARK_SUITES, build_query_suite_map, ensure_all_visible_after_load,
)

# --help presentation for suite groups.  A suite's group is the suites/<group>/
# subdirectory it lives in (None for top-level suites).  Maps a group id to its
# (argument-group title, description); unmapped groups fall back to "<id> suites".
SUITE_GROUP_HELP = {
    "regression": (
        "regression suites (read-stream concerns)",
        "Known regressions of particular concern -- read-stream / readahead "
        "behavior.  Each loads its own data if needed.",
    ),
    "workloads": (
        "workload suites (index types & scan patterns)",
        "Benchmarks exercising assorted index access methods and scan patterns "
        "(UUID, index-only heap fetches, GiST, SP-GiST, hash).  Each loads its "
        "own data if needed.",
    ),
}

# Output directory for results
# PREFETCH_OUTPUT_DIR overrides where results are read from and written to.
OUTPUT_DIR = os.environ.get("PREFETCH_OUTPUT_DIR", "prefetch_results")

# Global flag for using median vs min (set from args)
USE_MEDIAN = False


def get_representative(times):
    """Return the representative value (median or min) from a list of times."""
    if not times:
        return None
    return median(times) if USE_MEDIAN else min(times)


def get_stat_label():
    """Return the label for the representative statistic."""
    return "median" if USE_MEDIAN else "min"


# Dynamic per-query run count (see measure_query()).  We run RUNS_MIN times,
# then keep running until the total per-run WALL time -- cache eviction + prewarm
# + the timed query, not just the query -- reaches --runs-budget (default
# RUNS_BUDGET_MS), capped at RUNS_MAX.  RUNS_MIN is a hard floor.  Counting the
# eviction is deliberate: a query whose per-run wall is >= budget/3 gets just 3
# runs, so uncached queries with expensive heap eviction are not run dozens of
# times; cheaper-per-run queries (cached / index_only, whose only repeated cost
# is the query) extend toward the budget.  RUNS_MAX caps the very cheap queries
# (whose tiny per-run wall would otherwise let the budget buy hundreds of runs)
# at a point where the min has long since converged.
#
# A complementary EXECUTION-time budget (RUNS_EXEC_BUDGET_MS, --runs-exec-budget)
# runs alongside the wall budget: we also keep going while the total PURE QUERY
# time (the sum of the measured per-run times, eviction excluded) is below it.
# The two are OR'd -- a run happens while EITHER budget is unmet -- so a query
# whose expensive per-run eviction blows the wall budget at RUNS_MIN still gets
# enough runs to sample ~RUNS_EXEC_BUDGET_MS of actual execution (e.g. a ~110ms
# query gets ~5 runs instead of 3).  Both budgets are still capped at RUNS_MAX.
RUNS_MIN = 3
RUNS_MAX = 60
RUNS_BUDGET_MS = 800.0
RUNS_EXEC_BUDGET_MS = 500.0

# CPU pinning settings
BENCHMARK_CPU = 14

# --- Stress-test query generation probabilities ---
# These control the likelihood of various query features in randomly generated queries.
# Tune these to focus on patterns most likely to expose regressions.

STRESS_PROB_LATERAL_JOIN = 0.15        # Use LATERAL subquery (top-N per group)
STRESS_PROB_ANTI_JOIN = 0.10           # Use NOT EXISTS anti-join
STRESS_PROB_SEMI_JOIN = 0.15           # Use EXISTS semi-join
STRESS_PROB_CORRELATED_SUBQUERY = 0.10 # Correlated subquery in SELECT clause
STRESS_PROB_SELF_JOIN = 0.10           # Self-join on orders (exercises merge join mark/restore)
STRESS_PROB_FILTER_QUAL = 0.25         # Add filter qual that can't use index
STRESS_PROB_ORDER_BY = 0.70            # Include ORDER BY clause
STRESS_PROB_LIMIT = 0.50               # Include LIMIT clause (when ORDER BY present)
STRESS_PROB_AGGREGATE = 0.20           # Use count(*) or sum() aggregate
STRESS_PROB_INDEX_ONLY = 0.15          # Query only columns in index (index-only scan)
STRESS_PROB_MULTI_TABLE_JOIN = 0.30    # JOIN to dimension tables
STRESS_PROB_BACKWARDS_SCAN = 0.10      # Use DESC ordering (backwards index scan)
STRESS_PROB_IN_LIST = 0.15             # Use IN (...) instead of BETWEEN
STRESS_PROB_HIGH_SELECTIVITY = 0.30    # Very selective (few rows)
STRESS_PROB_LOW_SELECTIVITY = 0.20     # Low selectivity (many rows)
STRESS_PROB_SINGLE_DATE = 0.20         # Use exact date equality (more zero-row inner scans)
STRESS_PROB_FORCE_MERGE = 0.20         # Force MergeJoin on joins (disable hashjoin+nestloop)
STRESS_PROB_SORT_NON_INDEXED = 0.15    # ORDER BY non-indexed column (forces Sort above scan)

# Stress-test configuration
STRESS_QUERIES_PER_BATCH = 10          # Number of queries to generate per iteration
STRESS_REGRESSION_THRESHOLD = 1.05     # 5% slower = regression
STRESS_MIN_QUERY_MS = 1.5             # Discard queries slower than this (too noisy)


# io_max_concurrency is pinned high (below the io_uring per-ring ceiling) so that
# effective_io_concurrency is never clamped, for any io_method. It is not a CLI
# knob -- there is no benchmark reason to vary it; start_server() applies it.
IO_MAX_CONCURRENCY = 512

# Per-io-method defaults for the io-method-varying GUCs. Applied by
# resolve_io_method_defaults() only to options the user did NOT pass explicitly
# (argparse default=None sentinel). io_max_combine_limit is pinned to its maximum
# in start_server() and io_max_concurrency to IO_MAX_CONCURRENCY, so neither
# appears here. The worker-only knobs (io_max_workers, io_min_workers,
# io_worker_launch_interval) are inert under io_uring/sync but kept uniform.
IO_METHOD_DEFAULTS = {
    "io_uring": {
        "effective_io_concurrency": 100,
        "io_combine_limit": 16,
        "io_max_workers": 8,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
    "worker": {
        "effective_io_concurrency": 100,
        "io_combine_limit": 16,
        # Tuned: the io worker pool is the I/O-parallelism ceiling for
        # io_method=worker. At the stock 8, eic=100 was bottlenecked; raising it to
        # 32 (MAX_IO_WORKERS) cut the prefetch-suite geomean ~18% (0.451 -> 0.370)
        # and improved WR1/MU1/TD2 vs rel18. eic/icl/ilw stayed stock (raising
        # them hurt MU1/TD2).
        "io_max_workers": 32,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
    "sync": {
        "effective_io_concurrency": 0,
        "io_combine_limit": 16,
        "io_max_workers": 8,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
}


def resolve_io_method_defaults(args):
    """Fill any io GUC left as None (user did not pass it) from the per-io-method
    default table, keyed on args.io_method. An explicit flag value always wins."""
    table = IO_METHOD_DEFAULTS.get(args.io_method, IO_METHOD_DEFAULTS["io_uring"])
    for key, val in table.items():
        if getattr(args, key) is None:
            setattr(args, key, val)
    return args


def add_io_server_args(parser):
    """Register the server-start io GUC knobs.  Shared by prefetch_benchmark and
    perf_flamegraph so both harnesses start servers with identical settings --
    these feed start_server() and resolve_io_method_defaults().  Any knob left as
    None (sentinel) is filled per io_method by resolve_io_method_defaults(); an
    explicit value on the command line always wins."""
    parser.add_argument(
        "--effective_io_concurrency",
        type=int,
        default=None,
        help="effective_io_concurrency GUC (default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_combine_limit",
        type=int,
        default=None,
        help="io_combine_limit in 8kB units (default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_max_workers",
        type=int,
        default=None,
        help="io_max_workers GUC, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_min_workers",
        type=int,
        default=None,
        help="io_min_workers GUC, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_worker_launch_interval",
        type=int,
        default=None,
        help="io_worker_launch_interval GUC in ms, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_method",
        type=str,
        default="io_uring",
        choices=["io_uring", "worker", "sync"],
        help="Value for io_method PostgreSQL setting (default: io_uring)"
    )
    parser.add_argument(
        "--direct-io",
        action="store_true",
        dest="direct_io",
        help="Start Postgres with debug_io_direct=data and skip clear_cache.sh (O_DIRECT bypasses OS page cache)"
    )
    parser.add_argument(
        "--patch-direct",
        action="store_true",
        dest="patch_direct",
        help="Like --direct-io, but only for the testbranch server; the "
             "baseline keeps buffered I/O.  Direct I/O only makes sense with "
             "prefetching, so a direct-I/O baseline isn't a fair comparison "
             "point"
    )


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Prefetch benchmark for PostgreSQL index scan prefetching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                         # Run all queries, uncached, 3 runs
    %(prog)s --cached                # Run with data prewarmed
    %(prog)s --queries Q1,Q2,A1      # Run specific queries
    %(prog)s --queries WR1,Q1,GG1    # Queries spanning several suites at once
    %(prog)s --runs 5                # >=5 runs per query (per-query floor wins if higher)
    %(prog)s --skip-load             # Skip data loading verification
        """
    )
    # ----- workload selection: what to run, under what cache state ----- #
    g_workload = parser.add_argument_group("workload selection")
    g_workload.add_argument(
        "--cached",
        action="store_true",
        help="Run in cached mode (prewarm all relations instead of just indexes)"
    )
    g_workload.add_argument(
        "--queries",
        type=str,
        default=None,
        help="Comma-separated list of queries to run (e.g., Q1,Q2,A1). May span "
             "multiple suites (e.g. WR1,Q1,GG1); each owning suite is run with "
             "its subset, no suite flag needed. Default: all queries of the "
             "selected suite"
    )
    # Suite-selection flags (auto-generated from BENCHMARK_SUITES registry).
    # Suites that share a group (a suites/<group>/ subdirectory) get their own
    # labelled section in --help; ungrouped suites stay under workload selection.
    suite_arg_groups = {}  # group id -> argparse argument group
    for suite in BENCHMARK_SUITES:
        if suite["cli_flag"] is None:
            continue
        grp = suite["group"]
        if grp is None:
            target = g_workload
        else:
            if grp not in suite_arg_groups:
                title, desc = SUITE_GROUP_HELP.get(grp, (f"{grp} suites", None))
                suite_arg_groups[grp] = parser.add_argument_group(title, desc)
            target = suite_arg_groups[grp]
        target.add_argument(
            suite["cli_flag"],
            action="store_true",
            dest=suite["cli_dest"],
            help=suite["help"],
        )

    # ----- sampling: how many timed runs per query ----- #
    g_runs = parser.add_argument_group("sampling (run count)")
    g_runs.add_argument(
        "--runs", "--force-runs",
        dest="runs",
        type=int,
        default=None,
        metavar="N",
        help="Force exactly N runs per query, bypassing the dynamic run-count "
             "logic (even when N < 3). Default (unset): dynamic -- a minimum of 3 "
             "runs, extended until --runs-budget (total wall time: eviction + "
             "query) OR --runs-exec-budget (pure query time) is reached (up to "
             f"{RUNS_MAX}). (--force-runs is a backward-compatible alias.)"
    )
    g_runs.add_argument(
        "--runs-budget",
        dest="runs_budget",
        type=float,
        default=RUNS_BUDGET_MS,
        metavar="MS",
        help=f"Target total per-query wall-clock (ms) the dynamic run-count "
             f"spends before stopping (default: {RUNS_BUDGET_MS:.0f}) -- counting "
             f"cache eviction + prewarm + the timed query, not just the query, so "
             f"queries with expensive uncached eviction are not over-run. OR'd with "
             f"--runs-exec-budget (a run happens while EITHER is unmet). Clamped to "
             f"[{RUNS_MIN}, {RUNS_MAX}] runs. Ignored when an exact --runs count is "
             f"given."
    )
    g_runs.add_argument(
        "--runs-exec-budget",
        dest="runs_exec_budget",
        type=float,
        default=RUNS_EXEC_BUDGET_MS,
        metavar="MS",
        help=f"Complementary to --runs-budget: target total PURE QUERY time (ms) -- "
             f"the measured per-run times, eviction excluded -- the dynamic run-count "
             f"spends before stopping (default: {RUNS_EXEC_BUDGET_MS:.0f}). OR'd with "
             f"--runs-budget, so an eviction-heavy query that blows the wall budget "
             f"at {RUNS_MIN} runs still samples enough executions (e.g. a ~110ms query "
             f"gets ~5 runs). Clamped to [{RUNS_MIN}, {RUNS_MAX}] runs. Ignored when "
             f"an exact --runs count is given."
    )

    # ----- measurement method: how each query is timed ----- #
    g_method = parser.add_argument_group(
        "measurement method",
        "How each query is timed. Default: EXECUTE a prepared generic plan and "
        "discard the rows (no output formatting, get_actual_variable_range-clean).")
    g_method.add_argument(
        "--no-serialize",
        action="store_true",
        dest="no_serialize",
        help="Measure execution without output serialization. Default (EXECUTE) path: "
             "a cursor MOVE FORWARD ALL (execute + discard, no output formatting) on "
             "the raw query instead of EXECUTE. NOTE: this re-plans the query every "
             "run (DECLARE CURSOR has no prepared form), so unlike the default EXECUTE "
             "path it is not get_actual_variable_range-clean. --instrument path: drop "
             "the SERIALIZE EXPLAIN option."
    )
    g_method.add_argument(
        "--cursor",
        action="store_true",
        dest="cursor",
        help="Default-path modifier: read the result the way a real application "
             "does -- a server-side cursor iterated in fixed-size batches, "
             "materializing every row into Python objects as it arrives, so client "
             "decode overlaps server execution/prefetch (instead of libpq buffering "
             "the whole result first). Re-plans the raw query each call (not "
             "get_actual_variable_range-clean), which is intended: this models a "
             "realistic client, not clean executor timing. Ignored under --instrument "
             "and with --no-serialize."
    )
    g_method.add_argument(
        "--instrument",
        action="store_true",
        dest="instrument",
        help="Measure with EXPLAIN (ANALYZE, ..., IO) instead of the default "
             "EXECUTE-and-discard timing. Captures the plan + per-scan IO stats but "
             "adds executor instrumentation overhead that inflates the time (esp. for "
             "index-bound scans); the IO option is skipped on builds whose EXPLAIN "
             "lacks it (e.g. rel18)."
    )
    g_method.add_argument(
        "--timing",
        action="store_true",
        dest="instrument_timing",
        help="Enable per-node TIMING ON in the EXPLAIN ANALYZE measurement (more "
             "detail, more distortion; default is TIMING OFF). Implies --instrument."
    )

    # ----- result classification & reporting ----- #
    g_report = parser.add_argument_group("result classification & reporting")
    g_report.add_argument(
        "--noise-abs-ms",
        dest="noise_abs_ms",
        type=float,
        default=NOISE_ABS_MS,
        metavar="MS",
        help=f"Classification noise floor in ms (default {NOISE_ABS_MS:g}): a "
             f"per-query master-vs-patch DIFFERENCE this small is called neutral "
             f"regardless of the %% ratio or the query's runtime, so a regression "
             f"must be slower both by >1%% AND by >this many ms. Acts on the "
             f"delta, not the runtime."
    )
    g_report.add_argument(
        "--median",
        action="store_true",
        dest="use_median",
        help="Use median instead of min as the representative value in reports and comparisons"
    )
    g_report.add_argument(
        "--terse",
        action="store_true",
        help="Suppress per-run times and per-query detail; print only header, verification, and summary"
    )
    g_report.add_argument(
        "--no-confirm", "--no-reconfirm",
        action="store_true",
        dest="no_confirm",
        help="Skip the auto-confirmation pass. By default, queries that come out "
             "as a regression (patch slower than the baseline) are re-measured "
             "on both servers and the new samples pooled (min) into the result, "
             "so a one-off transient that spanned all of a query's runs is "
             "filtered out instead of being reported. Improvements (the expected "
             "direction) are not re-confirmed. Disabled automatically with an "
             "exact --runs count or --old-master-results."
    )

    # ----- builds compared ----- #
    g_builds = parser.add_argument_group("builds compared")
    g_builds.add_argument(
        "--baseline",
        type=str,
        choices=list(BASELINE_CONFIGS.keys()),
        default="master",
        help="Baseline PostgreSQL build to compare against (default: master). "
             "master runs against the testbranch's own data directory; a back "
             "branch has a dedicated one"
    )
    g_builds.add_argument(
        "--testbranch",
        type=str,
        choices=list(TESTBRANCH_CONFIGS.keys()),
        default="patch",
        help="PostgreSQL build to test (default: patch). Use 'master' to compare master vs rel18."
    )
    g_builds.add_argument(
        "--old-master-results",
        action="store_true",
        dest="old_master_results",
        help="Use baseline results from most recent benchmark run instead of re-running baseline"
    )

    # ----- patch prefetch configuration ----- #
    # By default only prefetch=on is tested on the patch.  The prefetch=off
    # comparison is opt-in (--include-no-prefetch for both, --prefetch-disabled
    # for off only).  args.prefetch_only is derived from these after parsing.
    g_prefetch = parser.add_argument_group(
        "patch prefetch configuration",
        "By default only prefetch=on is tested on the patch; these opt into the "
        "prefetch=off comparison.")
    prefetch_group = g_prefetch.add_mutually_exclusive_group()
    prefetch_group.add_argument(
        "--include-no-prefetch",
        action="store_true",
        dest="include_no_prefetch",
        help="Also test the patch with prefetching disabled (run both "
             "prefetch=on and prefetch=off). By default only prefetch=on runs."
    )
    prefetch_group.add_argument(
        "--prefetch-disabled",
        action="store_true",
        dest="prefetch_disabled",
        help="Only test patch with prefetching disabled (skip prefetch=on)"
    )
    prefetch_group.add_argument(
        "--prefetch-only",
        action="store_true",
        dest="prefetch_only_explicit",
        help="Only test patch with prefetching enabled (skip prefetch=off). "
             "This is the default; accepted for backward compatibility."
    )

    # ----- server & I/O configuration ----- #
    g_server = parser.add_argument_group("server & I/O configuration")
    # Server-start io GUC knobs, shared verbatim with perf_flamegraph.
    add_io_server_args(g_server)
    g_server.add_argument(
        "--delay-pgdata",
        action="store_true",
        dest="delay_pgdata",
        help="Use data directories with simulated I/O delay (data-delay instead of data)"
    )
    g_server.add_argument(
        "--no-tmpfs-hugepages",
        action="store_true",
        help="Disable tmpfs with huge=always (enabled by default)"
    )

    # ----- system & environment ----- #
    g_system = parser.add_argument_group("system & environment")
    g_system.add_argument(
        "--benchmark-cpu",
        type=int,
        default=BENCHMARK_CPU,
        help=f"CPU core to pin PostgreSQL backend to (default: {BENCHMARK_CPU})"
    )
    g_system.add_argument(
        "--pin",
        action="store_true",
        dest="pin",
        help="Enable CPU pinning and SCHED_FIFO for the backend process (off by default, can starve io_uring)"
    )
    g_system.add_argument(
        "--skip-load",
        action="store_true",
        dest="skip_load",
        help="Skip data loading verification (assume tables exist with correct data)"
    )
    g_system.add_argument(
        "--no-kill-interferers",
        action="store_true",
        dest="no_kill_interferers",
        help="Skip the kill_vscode_server.sh step entirely."
    )

    # ----- alternate modes ----- #
    g_modes = parser.add_argument_group("alternate modes")
    g_modes.add_argument(
        "--stress-test",
        action="store_true",
        dest="stress_test",
        help="Run stress test mode: randomly generate queries to find regressions"
    )
    g_modes.add_argument(
        "--min-query-ms",
        type=float,
        default=STRESS_MIN_QUERY_MS,
        dest="min_query_ms",
        help=f"Minimum query duration in ms for stress test (default: {STRESS_MIN_QUERY_MS}). "
             "Queries slower than this on master are discarded as too noisy."
    )
    g_modes.add_argument(
        "--list-modes",
        action="store_true",
        dest="list_modes",
        help="Output benchmark suite metadata as JSON (used by orchestration scripts)"
    )

    args = parser.parse_args()

    # When driven from inside a Claude Code session (CLAUDECODE is set in the
    # environment of every command it runs), auto-apply two conveniences so we
    # don't have to remember the flags -- and, more importantly, so we never
    # tear down the very session running the benchmark:
    #   * force --no-kill-interferers: kill_vscode_server.sh kills the 'claude'
    #     and 'vscode-server' targets under --force, which would kill us.
    #   * default --skip-load: the agent workflow reuses already-loaded data.
    # Both remain explicitly settable; this only strengthens the safe default.
    if os.environ.get("CLAUDECODE"):
        if not args.no_kill_interferers:
            print("CLAUDECODE detected: forcing --no-kill-interferers so the "
                  "driving claude/vscode session is not killed.")
            args.no_kill_interferers = True
        if not args.skip_load:
            print("CLAUDECODE detected: assuming --skip-load (reusing "
                  "already-loaded data).")
            args.skip_load = True

    # Prefetch=on only is the default; the prefetch=off run is opt-in.
    # --include-no-prefetch runs both; --prefetch-disabled runs off only.
    # Downstream code reads args.prefetch_only / args.prefetch_disabled.
    args.prefetch_only = not (args.include_no_prefetch or args.prefetch_disabled)

    # The timing-method flags only affect the default benchmark path (run_query
    # threads them through).  --stress-test always uses run_query's defaults --
    # the authoritative EXECUTE-discard timing -- so reject these flags there
    # rather than silently ignoring them.
    if args.stress_test:
        bad = [flag for flag, on in (("--no-serialize", args.no_serialize),
                                      ("--cursor", args.cursor),
                                      ("--instrument", args.instrument),
                                      ("--timing", args.instrument_timing))
               if on]
        if bad:
            parser.error(f"--stress-test does not support {', '.join(bad)}; it always "
                         f"uses the default EXECUTE-discard timing")

    # --timing is a detail level of the EXPLAIN ANALYZE path, so it implies
    # --instrument (the path that consumes per-node TIMING).
    if args.instrument_timing:
        args.instrument = True

    if args.direct_io and args.patch_direct:
        parser.error("--patch-direct conflicts with --direct-io "
                     "(--direct-io already runs both servers with direct I/O)")

    return args


# --- Stress-test query generation ---

def random_date_range():
    """Generate a random date range within 2023."""
    # Start date: random day in 2023
    start_day = random.randint(1, 330)  # Leave room for range
    start_date = datetime(2023, 1, 1) + timedelta(days=start_day)

    # Determine range size based on selectivity
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        # Narrow range: 1-3 days (high selectivity)
        days = random.randint(1, 3)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        # Wide range: 30-90 days (low selectivity)
        days = random.randint(30, 90)
    else:
        # Medium range: 5-20 days
        days = random.randint(5, 20)

    end_date = start_date + timedelta(days=days)
    return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')


def random_customer_range():
    """Generate a random customer_id range."""
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        # Single customer or very small range
        start = random.randint(1, 100000)
        count = random.randint(1, 5)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        # Large range: 5000-20000 customers
        start = random.randint(1, 80000)
        count = random.randint(5000, 20000)
    else:
        # Medium range: 100-1000 customers
        start = random.randint(1, 99000)
        count = random.randint(100, 1000)

    end = min(start + count, 100000)
    return start, end


def random_product_range():
    """Generate a random product_id range."""
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        start = random.randint(1, 10000)
        count = random.randint(1, 10)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        start = random.randint(1, 5000)
        count = random.randint(2000, 5000)
    else:
        start = random.randint(1, 9000)
        count = random.randint(50, 500)

    end = min(start + count, 10000)
    return start, end


def random_in_list(start, end, max_items=10):
    """Generate a random IN list from a range."""
    count = min(random.randint(3, max_items), end - start + 1)
    values = random.sample(range(start, end + 1), count)
    return ', '.join(str(v) for v in sorted(values))


def random_filter_qual(table_alias=""):
    """Generate a random filter qual that can't use the index."""
    prefix = f"{table_alias}." if table_alias else ""
    qual_type = random.choice(['amount_gt', 'amount_between', 'region_in'])

    if qual_type == 'amount_gt':
        threshold = random.randint(100, 900)
        return f"{prefix}amount > {threshold}"
    elif qual_type == 'amount_between':
        low = random.randint(0, 500)
        high = low + random.randint(100, 400)
        return f"{prefix}amount BETWEEN {low} AND {high}"
    else:  # region_in
        regions = random.sample(range(1, 21), random.randint(2, 5))
        return f"{prefix}region_id IN ({', '.join(str(r) for r in regions)})"


def random_limit():
    """Generate a random LIMIT value."""
    if random.random() < 0.3:
        return random.randint(1, 10)  # Very small
    elif random.random() < 0.5:
        return random.randint(100, 1000)  # Medium
    else:
        return random.randint(5000, 50000)  # Large


def generate_random_query(query_num):
    """
    Generate a random query targeting the prefetch benchmark tables.
    Returns a query definition dict compatible with the suite query format.
    """
    # Decide query type based on probabilities
    # These are mutually exclusive special patterns
    use_lateral = random.random() < STRESS_PROB_LATERAL_JOIN
    use_anti_join = not use_lateral and random.random() < STRESS_PROB_ANTI_JOIN
    use_semi_join = not use_lateral and not use_anti_join and random.random() < STRESS_PROB_SEMI_JOIN
    use_correlated = not use_lateral and not use_anti_join and not use_semi_join and random.random() < STRESS_PROB_CORRELATED_SUBQUERY
    use_self_join = not use_lateral and not use_anti_join and not use_semi_join and not use_correlated and random.random() < STRESS_PROB_SELF_JOIN
    use_aggregate = not use_lateral and not use_self_join and random.random() < STRESS_PROB_AGGREGATE

    # Independent features
    use_filter_qual = random.random() < STRESS_PROB_FILTER_QUAL
    use_order_by = random.random() < STRESS_PROB_ORDER_BY
    use_limit = use_order_by and random.random() < STRESS_PROB_LIMIT
    use_backwards = use_order_by and random.random() < STRESS_PROB_BACKWARDS_SCAN
    use_in_list = random.random() < STRESS_PROB_IN_LIST
    use_index_only = random.random() < STRESS_PROB_INDEX_ONLY
    use_multi_join = random.random() < STRESS_PROB_MULTI_TABLE_JOIN
    use_single_date = random.random() < STRESS_PROB_SINGLE_DATE
    use_force_merge = random.random() < STRESS_PROB_FORCE_MERGE
    use_sort_non_indexed = use_order_by and random.random() < STRESS_PROB_SORT_NON_INDEXED

    # Generate date range (used in most queries)
    date_start, date_end = random_date_range()
    cust_start, cust_end = random_customer_range()
    prod_start, prod_end = random_product_range()

    # Build date condition: single-date equality or BETWEEN range.
    # Single-date creates more zero-row inner scans (only ~0.7 orders/customer/day).
    if use_single_date:
        date_cond = f"order_date = '{date_start}'"
        date_cond_aliased = lambda alias: f"{alias}.order_date = '{date_start}'"
    else:
        date_cond = f"order_date BETWEEN '{date_start}' AND '{date_end}'"
        date_cond_aliased = lambda alias: f"{alias}.order_date BETWEEN '{date_start}' AND '{date_end}'"

    # Build query based on type
    evict = ["prefetch_orders"]
    prewarm_indexes = []
    prewarm_tables = ["prefetch_orders"]
    query_features = []
    gucs = {}

    if use_lateral:
        # LATERAL join: top-N per customer
        query_features.append("LATERAL")
        limit_val = random.randint(3, 10)
        order_dir = "DESC" if use_backwards else ""

        if use_in_list and (cust_end - cust_start) <= 20:
            cust_cond = f"c.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"c.customer_id BETWEEN {cust_start} AND {cust_end}"

        inner_filter = ""
        if use_filter_qual:
            # Inside LATERAL subquery, prefetch_orders has no alias, so no prefix needed
            # But avoid region_id which would be ambiguous - use amount only
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                inner_filter = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                inner_filter = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        sql = f"""
            SELECT c.customer_id, o.order_id, o.order_date, o.amount
            FROM prefetch_customers c,
            LATERAL (
                SELECT order_id, order_date, amount
                FROM prefetch_orders
                WHERE customer_id = c.customer_id
                  AND {date_cond}
                  {inner_filter}
                ORDER BY order_date {order_dir}
                LIMIT {limit_val}
            ) o
            WHERE {cust_cond}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

    elif use_anti_join:
        # NOT EXISTS anti-join
        query_features.append("anti-join")

        if use_in_list and (cust_end - cust_start) <= 20:
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        region_id = random.randint(1, 20)

        select_cols = "o.customer_id, o.order_date" if use_index_only else "o.order_id, o.customer_id, o.amount"
        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_cols}
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND {cust_cond}
              AND NOT EXISTS (
                  SELECT 1 FROM prefetch_customers c
                  WHERE c.customer_id = o.customer_id
                    AND c.region_id = {region_id}
              )
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n            LIMIT {random_limit()}"

    elif use_semi_join:
        # EXISTS semi-join
        query_features.append("semi-join")

        region_id = random.randint(1, 20)

        select_cols = "o.customer_id, o.order_date" if use_index_only else "o.order_id, o.customer_id, o.amount"
        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_cols}
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND EXISTS (
                  SELECT 1 FROM prefetch_customers c
                  WHERE c.customer_id = o.customer_id
                    AND c.region_id = {region_id}
              )
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n            LIMIT {random_limit()}"

    elif use_correlated:
        # Correlated subquery in SELECT
        query_features.append("correlated")

        if use_in_list and (cust_end - cust_start) <= 20:
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        filter_clause = ""
        if use_filter_qual:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT o.order_id, o.customer_id, o.amount,
                   (SELECT c.customer_name FROM prefetch_customers c
                    WHERE c.customer_id = o.customer_id) as cust_name
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND {cust_cond}
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n            LIMIT {random_limit()}"

    elif use_aggregate:
        # Aggregate query
        query_features.append("aggregate")
        agg_type = random.choice(['count', 'sum', 'both'])

        if agg_type == 'count':
            select_clause = "order_date, count(*) as cnt"
        elif agg_type == 'sum':
            select_clause = "order_date, sum(amount) as total"
        else:
            select_clause = "order_date, count(*) as cnt, sum(amount) as total"

        filter_clause = ""
        if use_filter_qual:
            # No table alias in aggregate query, so no prefix needed
            # Avoid region_id ambiguity by only using amount
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                filter_clause = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                filter_clause = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_clause}
            FROM prefetch_orders
            WHERE {date_cond}
              {filter_clause}
            GROUP BY order_date
        """
        prewarm_indexes = ["prefetch_orders_date_idx"]

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            # Aggregate GROUP BY order_date: sort-non-indexed not applicable
            sql = sql.rstrip() + f"\n            ORDER BY order_date {order_dir}"

    elif use_multi_join:
        # Multi-table JOIN
        query_features.append("JOIN")
        join_to = random.choice(['customers', 'products', 'both'])

        if use_in_list and (cust_end - cust_start) <= 20:
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        filter_clause = ""
        if use_filter_qual:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        if join_to == 'customers':
            sql = f"""
                SELECT o.order_id, c.customer_name, o.amount, o.order_date
                FROM prefetch_orders o
                JOIN prefetch_customers c ON c.customer_id = o.customer_id
                WHERE {cust_cond}
                  AND {date_cond_aliased('o')}
                  {filter_clause}
            """
            evict.append("prefetch_customers")
            prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
            prewarm_tables.append("prefetch_customers")
        elif join_to == 'products':
            sql = f"""
                SELECT o.order_id, p.product_name, o.amount, o.order_date
                FROM prefetch_orders o
                JOIN prefetch_products p ON p.product_id = o.product_id
                WHERE o.product_id BETWEEN {prod_start} AND {prod_end}
                  AND {date_cond_aliased('o')}
                  {filter_clause}
            """
            evict.append("prefetch_products")
            prewarm_indexes = ["prefetch_orders_prod_idx", "prefetch_products_pkey"]
            prewarm_tables.append("prefetch_products")
        else:  # both
            query_features.append("multi-JOIN")
            sql = f"""
                SELECT o.order_id, c.customer_name, p.product_name, o.amount
                FROM prefetch_orders o
                JOIN prefetch_customers c ON c.customer_id = o.customer_id
                JOIN prefetch_products p ON p.product_id = o.product_id
                WHERE {date_cond_aliased('o')}
                  AND {cust_cond}
                  {filter_clause}
            """
            evict.extend(["prefetch_customers", "prefetch_products"])
            prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey", "prefetch_products_pkey"]
            prewarm_tables.extend(["prefetch_customers", "prefetch_products"])

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n                ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n                LIMIT {random_limit()}"

        # Force merge join on multi-table JOINs
        if use_force_merge:
            gucs["enable_hashjoin"] = "off"
            gucs["enable_nestloop"] = "off"
            query_features.append("merge-forced")

    elif use_self_join:
        # Self-join on orders: both sides use same index, can trigger
        # MergeJoin mark/restore when outer has duplicate join keys.
        query_features.append("self-join")

        # Generate two separate date ranges for the two sides
        date_start2, date_end2 = random_date_range()

        if use_in_list and (cust_end - cust_start) <= 20:
            cust_cond = f"o1.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o1.customer_id BETWEEN {cust_start} AND {cust_end}"

        date_cond1 = (f"o1.order_date = '{date_start}'"
                      if use_single_date else
                      f"o1.order_date BETWEEN '{date_start}' AND '{date_end}'")
        date_cond2 = (f"o2.order_date = '{date_start2}'"
                      if use_single_date else
                      f"o2.order_date BETWEEN '{date_start2}' AND '{date_end2}'")
        if use_single_date:
            query_features.append("single-date")

        sql = f"""
            SELECT count(*)
            FROM prefetch_orders o1
            JOIN prefetch_orders o2 ON o1.customer_id = o2.customer_id
            WHERE {cust_cond}
              AND {date_cond1}
              AND {date_cond2}
        """
        prewarm_indexes = ["prefetch_orders_cust_date_idx"]

        # Always force merge join on self-joins — that's the point
        gucs["enable_hashjoin"] = "off"
        gucs["enable_nestloop"] = "off"
        query_features.append("merge-forced")

    else:
        # Simple range scan on prefetch_orders
        query_features.append("range-scan")

        # Choose which index to target
        index_choice = random.choice(['date', 'cust_date', 'product'])

        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            # No table alias in simple range scan, avoid region_id ambiguity
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                filter_clause = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                filter_clause = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        if index_choice == 'date':
            select_cols = "order_date" if use_index_only else "order_id, customer_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {date_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_date_idx"]

        elif index_choice == 'cust_date':
            if use_in_list and (cust_end - cust_start) <= 20:
                cust_cond = f"customer_id IN ({random_in_list(cust_start, cust_end)})"
            else:
                cust_cond = f"customer_id BETWEEN {cust_start} AND {cust_end}"

            select_cols = "customer_id, order_date" if use_index_only else "order_id, customer_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {cust_cond}
                  AND {date_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_cust_date_idx"]

        else:  # product
            if use_in_list and (prod_end - prod_start) <= 20:
                prod_cond = f"product_id IN ({random_in_list(prod_start, prod_end)})"
            else:
                prod_cond = f"product_id BETWEEN {prod_start} AND {prod_end}"

            select_cols = "product_id" if use_index_only else "order_id, product_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {prod_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_prod_idx"]

        if use_order_by:
            if use_sort_non_indexed:
                order_col = "amount"
            else:
                order_col = "order_date" if index_choice in ['date', 'cust_date'] else "product_id"
            order_dir = "DESC" if use_backwards else ""
            sql = sql.rstrip() + f"\n                ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n                LIMIT {random_limit()}"

    # Add feature tags for independent modifiers
    if use_backwards:
        query_features.append("backwards")
    if use_single_date and not use_self_join:  # self-join already tags it
        query_features.append("single-date")
    if use_sort_non_indexed:
        query_features.append("sort-non-indexed")

    # Build name from features
    name = f"Stress #{query_num}: {', '.join(query_features)}"

    result = {
        "name": name,
        "sql": sql,
        "evict": list(set(evict)),  # Remove duplicates
        "prewarm_indexes": prewarm_indexes,
        "prewarm_tables": list(set(prewarm_tables)),
    }
    if gucs:
        result["gucs"] = gucs
    return result


def get_git_hash(source_dir):
    """Get the current git commit hash for a source directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def get_pg_version(conn_details):
    """Get PostgreSQL version string."""
    try:
        conn = psycopg.connect(**conn_details, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
        conn.close()
        return version
    except Exception as e:
        return f"error: {e}"


def print_io_settings(args):
    """Print the PostgreSQL I/O settings that will be used."""
    print(f"\nPostgreSQL I/O settings:")
    print(f"  effective_io_concurrency = {args.effective_io_concurrency}")
    print(f"  io_combine_limit = {args.io_combine_limit} (8kB units)")
    print(f"  io_max_combine_limit = 128 (8kB units, pinned to max)")
    print(f"  io_max_concurrency = {IO_MAX_CONCURRENCY}")
    print(f"  io_max_workers = {args.io_max_workers}")
    print(f"  io_min_workers = {args.io_min_workers}")
    print(f"  io_worker_launch_interval = {args.io_worker_launch_interval} (ms)")
    print(f"  io_method = {args.io_method}")
    if args.direct_io:
        print(f"  debug_io_direct = data")
    elif args.patch_direct:
        print(f"  debug_io_direct = data (testbranch only; baseline buffered)")


def server_direct_io(args, on_patch):
    """Whether the server on the given side runs with debug_io_direct=data.

    --direct-io puts both servers in direct-I/O mode; --patch-direct puts only
    the testbranch server there, leaving the baseline buffered.
    """
    return args.direct_io or (on_patch and args.patch_direct)


def server_running(pg_bin_dir, pg_data_dir):
    """Whether a postmaster is running on pg_data_dir (pg_ctl status exits 0).
    False when pg_bin_dir has no pg_ctl: the missing binary is reported by
    whoever tries to use it next."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    try:
        result = subprocess.run([pg_ctl_path, "status", "-D", pg_data_dir],
                                capture_output=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def ensure_server_stopped(pg_bin_dir, pg_name, pg_data_dir):
    """Stop whatever postmaster is running on pg_data_dir, so that a fresh
    start_server() owns it."""
    if not server_running(pg_bin_dir, pg_data_dir):
        return
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    print(f"{pg_name}: Server is already running. Stopping it...")
    subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
    time.sleep(2)


def print_log_tail(log_file, lines=20):
    """Print the last lines of a server log; pg_ctl's own failure message only
    says to examine the log output."""
    try:
        with open(log_file, errors="replace") as f:
            tail = f.readlines()[-lines:]
    except OSError:
        return
    if tail:
        print(f"Last {len(tail)} lines of {log_file}:")
        for line in tail:
            print(f"  {line.rstrip()}")


# The FATAL message a postmaster prints when its binary cannot open a data
# directory -- a different major version, PG_CONTROL_VERSION or
# CATALOG_VERSION_NO; the DETAIL line that follows names the two versions.
INCOMPATIBLE_DATA_DIR_MSG = "database files are incompatible with server"


def preflight_data_dir(pg_bin_dir, pg_name, pg_data_dir):
    """Check that the server binary in pg_bin_dir can open pg_data_dir, and
    exit with a clear message if it cannot.  Meant to run before anything else
    touches the machine (tmpfs setup, killing interferers, data loading, the
    first server start).

    'postgres -C <runtime-computed GUC>' runs postmaster startup only as far
    as reading the control file -- exactly where a data directory of a
    different major version, PG_CONTROL_VERSION or CATALOG_VERSION_NO is
    rejected -- then exits.  No server starts, no recovery runs, and nothing
    in the data directory changes beyond the lock file taken and released.
    It takes milliseconds.

    A mismatch means one side is behind the other, and the message says
    which: a binary older than the data directory is a stale install
    (typically master pulled past a catversion bump but never rebuilt and
    reinstalled), a data directory older than the binary needs recreating
    (a delay cluster left behind by a catversion bump, say).  With master
    sharing the patch build's data directory, the stale master install is the
    failure to catch up front; it would otherwise surface only at master's
    first start, after the tmpfs setup and a possible data load.
    """
    postgres_path = os.path.join(pg_bin_dir, "postgres")
    if not os.path.isfile(postgres_path):
        output = f"{postgres_path}: no such file"
    elif server_running(pg_bin_dir, pg_data_dir):
        # The probe needs the lock file.  start_server() stops the server,
        # and its own start then applies the same checks.
        print(f"{pg_name}: a server is running on {pg_data_dir}; the "
              f"binary/data-directory check happens when it is restarted")
        return
    else:
        result = subprocess.run(
            [postgres_path, "-D", pg_data_dir, "-C", "data_checksums"],
            capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        output = (result.stdout + result.stderr).strip()
    line = "!" * 72
    print(f"\n{line}")
    print(f"!! ERROR: the {pg_name} server binary cannot open its data directory")
    print(f"!!   binary:   {postgres_path}")
    print(f"!!   data dir: {pg_data_dir}")
    if INCOMPATIBLE_DATA_DIR_MSG in output:
        detail = re.search(r"DETAIL:\s*(.*)", output)
        detail = detail.group(1).strip() if detail else ""
        print(f"!!   {detail}")
        # Which side is behind?  The DETAIL names the two version numbers.
        versions = re.search(r"initialized with (\w+) (\d+), but the server "
                             r"was compiled with \1 (\d+)", detail)
        if versions and int(versions.group(3)) < int(versions.group(2)):
            print("!! The binary is older than the data directory: the "
                  "install is probably stale.  Rebuild and reinstall this "
                  "build, then retry.")
        elif versions:
            print("!! The data directory is older than the binary: it needs "
                  "to be recreated (or reloaded from a build that matches it).")
        else:
            print("!! This build and data directory do not belong together "
                  "(a different major version cannot open the cluster).")
    else:
        for out_line in output.splitlines():
            print(f"!!   {out_line}")
    print(f"{line}\n")
    sys.exit(1)


def preflight_servers(args):
    """preflight_data_dir() for the baseline and testbranch servers a run will
    start.  The baseline is skipped under --old-master-results, which never
    starts it (the stress test ignores that flag and always does)."""
    checks = []
    if not (args.old_master_results and not args.stress_test):
        bin_dir, data_dir, _source_dir, _conn = BASELINE_CONFIGS[args.baseline]
        checks.append((args.baseline, bin_dir, data_dir))
    bin_dir, data_dir, _source_dir, _conn = TESTBRANCH_CONFIGS[args.testbranch]
    checks.append((args.testbranch, bin_dir, data_dir))
    for name, bin_dir, data_dir in checks:
        preflight_data_dir(bin_dir, name, data_dir)


def start_server(pg_bin_dir, pg_name, pg_data_dir, conn_details, args, on_patch):
    """Start a PostgreSQL server and wait for it to be ready."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    log_file = os.path.join(OUTPUT_DIR, f"{pg_name}.postgres_log")

    ensure_server_stopped(pg_bin_dir, pg_name, pg_data_dir)

    # Start the server
    print(f"Starting {pg_name} PostgreSQL server (port {conn_details.get('port', 'default')})...")
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    # Build PostgreSQL configuration options
    pg_options = [
        "--autovacuum=off",
        "-c wal_level=minimal",
        "-c max_wal_senders=0",
        "-c max_parallel_workers_per_gather=0",
        # The benchmark only ever uses a couple of connections. A low
        # max_connections keeps MaxBackends small, which (a) lets io_method=io_uring
        # fit its per-backend rings under RLIMIT_MEMLOCK even at high
        # io_max_concurrency, and (b) shrinks shared memory / huge-page footprint.
        # Keep this value in sync with set-pg-hugepages.sh (the huge-page
        # reservation).
        "-c max_connections=20",
        f"-c effective_io_concurrency={args.effective_io_concurrency}",
        f"-c io_combine_limit={args.io_combine_limit}",
        # Pin the server-wide ceiling to its maximum and rely purely on the
        # per-backend io_combine_limit; we don't care about server-wide limits here.
        "-c io_max_combine_limit=128",
        f"-c io_max_concurrency={IO_MAX_CONCURRENCY}",
        f"-c io_method={args.io_method}",
    ]

    # Detect whether this binary uses the old io_workers GUC or the new
    # io_min_workers/io_max_workers pair (changed in commit d1c01b79d).
    postgres_path = os.path.join(pg_bin_dir, "postgres")
    probe = subprocess.run([postgres_path, "--describe-config"],
                           capture_output=True, text=True, check=False)
    if "io_max_workers" in probe.stdout:
        pg_options.append(f"-c io_max_workers={args.io_max_workers}")
        pg_options.append(f"-c io_min_workers={args.io_min_workers}")
    elif "io_workers" in probe.stdout:
        pg_options.append(f"-c io_workers={args.io_max_workers}")
    # Worker-only; harmless (inert) under io_uring/sync.
    if "io_worker_launch_interval" in probe.stdout:
        pg_options.append(f"-c io_worker_launch_interval={args.io_worker_launch_interval}")
    if server_direct_io(args, on_patch):
        pg_options.append("-c debug_io_direct=data")

    cmd = [pg_ctl_path, "start", "-D", pg_data_dir, "-l", log_file]
    for opt in pg_options:
        cmd.extend(["-o", opt])
    if start_options:
        cmd.extend(["-o", start_options])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: Failed to start {pg_name} server")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        print_log_tail(log_file)
        sys.exit(1)

    # Wait for the server to be ready
    for attempt in range(15):
        try:
            conn = psycopg.connect(**conn_details, connect_timeout=2)
            conn.close()
            return
        except (psycopg.OperationalError, psycopg.DatabaseError):
            if attempt == 14:
                print(f"Error: {pg_name} server failed to start after 15 attempts")
                sys.exit(1)
            time.sleep(0.5 if attempt < 5 else 1)


def stop_server(pg_bin_dir, pg_data_dir):
    """Stop a PostgreSQL server."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=False)


# Relations to sync statistics for
STATS_RELATIONS = [
    # Tables
    'prefetch_orders', 'prefetch_customers', 'prefetch_products',
    'prefetch_sequential', 'prefetch_sparse',
    # Indexes
    'prefetch_orders_cust_date_idx', 'prefetch_orders_date_idx',
    'prefetch_orders_prod_idx', 'prefetch_orders_id_idx',
    'prefetch_sequential_idx', 'prefetch_sparse_cat_idx',
    'prefetch_customers_pkey', 'prefetch_products_pkey',
]


def extract_statistics(conn_details):
    """
    Extract optimizer statistics from a PostgreSQL database.
    Returns a dictionary containing relation and attribute stats.
    """
    conn = psycopg.connect(**conn_details)
    stats = {
        'relation_stats': [],
        'attribute_stats': []
    }

    # Extract relation-level stats from pg_class
    with conn.cursor() as cur:
        for relname in STATS_RELATIONS:
            cur.execute("""
                SELECT c.relname, n.nspname,
                       c.relpages, c.reltuples,
                       c.relallvisible, c.relallfrozen
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = %s AND n.nspname = 'public'
            """, (relname,))
            row = cur.fetchone()
            if row:
                stats['relation_stats'].append({
                    'relname': row[0],
                    'schemaname': row[1],
                    'relpages': row[2],
                    'reltuples': row[3],
                    'relallvisible': row[4],
                    'relallfrozen': row[5],
                })

    # Extract attribute-level stats from pg_stats
    with conn.cursor() as cur:
        for relname in STATS_RELATIONS:
            cur.execute("""
                SELECT schemaname, tablename, attname, inherited,
                       null_frac, avg_width, n_distinct,
                       most_common_vals::text, most_common_freqs,
                       histogram_bounds::text, correlation,
                       most_common_elems::text, most_common_elem_freqs,
                       elem_count_histogram
                FROM pg_stats
                WHERE tablename = %s AND schemaname = 'public'
            """, (relname,))
            for row in cur.fetchall():
                stats['attribute_stats'].append({
                    'schemaname': row[0],
                    'tablename': row[1],
                    'attname': row[2],
                    'inherited': row[3],
                    'null_frac': row[4],
                    'avg_width': row[5],
                    'n_distinct': row[6],
                    'most_common_vals': row[7],
                    'most_common_freqs': list(row[8]) if row[8] else None,
                    'histogram_bounds': row[9],
                    'correlation': row[10],
                    'most_common_elems': row[11],
                    'most_common_elem_freqs': list(row[12]) if row[12] else None,
                    'elem_count_histogram': list(row[13]) if row[13] else None,
                })

    conn.close()
    print(f"Extracted stats for {len(stats['relation_stats'])} relations, "
          f"{len(stats['attribute_stats'])} attributes")
    return stats


def _sql_literal(value, type_cast=None):
    """Convert a Python value to a SQL literal string."""
    if value is None:
        return 'NULL' + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, bool):
        return ('true' if value else 'false') + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, (int, float)):
        return str(value) + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, list):
        # Format as PostgreSQL array literal
        elements = ', '.join(str(v) for v in value)
        return f"ARRAY[{elements}]" + (f'::{type_cast}' if type_cast else '')
    else:
        # String - escape single quotes
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'" + (f'::{type_cast}' if type_cast else '')


def restore_statistics(conn_details, stats):
    """
    Restore optimizer statistics to a PostgreSQL database.
    Uses pg_restore_relation_stats and pg_restore_attribute_stats.
    """
    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    restored_rels = 0
    restored_attrs = 0

    # Restore relation-level stats
    with conn.cursor() as cur:
        for rs in stats['relation_stats']:
            try:
                sql = f"""
                    SELECT pg_restore_relation_stats(
                        'schemaname', {_sql_literal(rs['schemaname'])},
                        'relname', {_sql_literal(rs['relname'])},
                        'relpages', {_sql_literal(rs['relpages'], 'integer')},
                        'reltuples', {_sql_literal(rs['reltuples'], 'real')},
                        'relallvisible', {_sql_literal(rs['relallvisible'], 'integer')},
                        'relallfrozen', {_sql_literal(rs['relallfrozen'], 'integer')}
                    )
                """
                cur.execute(sql)
                restored_rels += 1
            except Exception as e:
                print(f"Warning: Failed to restore relation stats for {rs['relname']}: {e}")

    # Restore attribute-level stats
    with conn.cursor() as cur:
        for ats in stats['attribute_stats']:
            try:
                sql = f"""
                    SELECT pg_restore_attribute_stats(
                        'schemaname', {_sql_literal(ats['schemaname'])},
                        'relname', {_sql_literal(ats['tablename'])},
                        'attname', {_sql_literal(ats['attname'])},
                        'inherited', {_sql_literal(ats['inherited'], 'boolean')},
                        'null_frac', {_sql_literal(ats['null_frac'], 'real')},
                        'avg_width', {_sql_literal(ats['avg_width'], 'integer')},
                        'n_distinct', {_sql_literal(ats['n_distinct'], 'real')},
                        'most_common_vals', {_sql_literal(ats['most_common_vals'], 'text')},
                        'most_common_freqs', {_sql_literal(ats['most_common_freqs'], 'real[]')},
                        'histogram_bounds', {_sql_literal(ats['histogram_bounds'], 'text')},
                        'correlation', {_sql_literal(ats['correlation'], 'real')},
                        'most_common_elems', {_sql_literal(ats['most_common_elems'], 'text')},
                        'most_common_elem_freqs', {_sql_literal(ats['most_common_elem_freqs'], 'real[]')},
                        'elem_count_histogram', {_sql_literal(ats['elem_count_histogram'], 'real[]')}
                    )
                """
                cur.execute(sql)
                restored_attrs += 1
            except Exception as e:
                print(f"Warning: Failed to restore attribute stats for "
                      f"{ats['tablename']}.{ats['attname']}: {e}")

    conn.close()
    print(f"Restored stats for {restored_rels} relations, {restored_attrs} attributes")


def extract_execution_time(explain_output):
    """Extract execution time from EXPLAIN ANALYZE output."""
    for line in explain_output:
        match = re.search(r'Execution Time: ([\d.]+) ms', line[0])
        if match:
            return float(match.group(1))
    return None


def pin_backend(pid, cpu, enabled=False):
    """Pin a backend process to a specific CPU."""
    if not enabled:
        return
    try:
        result = subprocess.run(
            ["taskset", "-cp", str(cpu), str(pid)],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            print(f"Pinned backend PID {pid} to CPU {cpu}")

        # Try RT scheduling
        result = subprocess.run(
            ["sudo", "chrt", "-f", "-p", "1", str(pid)],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            print(f"Set backend PID {pid} to SCHED_FIFO")
    except Exception as e:
        print(f"Warning: Could not pin backend: {e}")


PREPARED_STMT_NAME = "_bench_stmt"

# Name of the prepared statement currently set up by prepare_query().  Only one
# benchmark statement is ever prepared at a time (prepare -> run -> deallocate),
# so run_query()/deallocate_query() act on whichever statement is active here.
# prepare_query() can give it a descriptive, query-specific name so the EXECUTE
# (and thus the backend's debug_query_string) identifies which benchmark query
# is running -- handy for tying server-side log output back to a specific query.
_active_stmt_name = PREPARED_STMT_NAME


def bench_stmt_name(query_id):
    """Build a descriptive, SQL-safe prepared-statement name for a query."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(query_id))
    return f"{PREPARED_STMT_NAME}_{safe}"


def prepare_query(conn, sql, gucs=None, is_master=False, prefetch_setting=None,
                  stmt_name=None):
    """Prepare a query so that subsequent runs avoid planning overhead.

    Planning-relevant GUCs must be active before PREPARE, since a
    parameter-less prepared statement is planned once at PREPARE time.
    We also force generic plan mode so EXECUTE reuses the cached plan
    without per-execution plan cache overhead.

    stmt_name optionally gives the statement a descriptive name (e.g. derived
    from the query id via bench_stmt_name); run_query()/deallocate_query() then
    operate on that same name.
    """
    global _active_stmt_name
    _active_stmt_name = stmt_name or PREPARED_STMT_NAME
    set_gucs(conn, gucs or {}, is_master=is_master, prefetch_setting=prefetch_setting)
    with conn.cursor() as cur:
        cur.execute("SET plan_cache_mode = force_generic_plan")
        cur.execute(f"PREPARE {_active_stmt_name} AS {sql.strip()}")


def deallocate_query(conn):
    """Deallocate the prepared benchmark statement."""
    with conn.cursor() as cur:
        cur.execute(f"DEALLOCATE {_active_stmt_name}")


def explain_supports_io(conn_details):
    """Return True if this server's EXPLAIN accepts the IO option.

    The IO option (per-scan I/O instrumentation) comes from the index-prefetch
    work and the master dev tree; stock PG 18 / rel18 rejects it as an
    unrecognized EXPLAIN option.  Probe on a throwaway connection so a rejection
    can't leave the benchmark connection in an aborted-transaction state.
    """
    try:
        probe = psycopg.connect(**conn_details, connect_timeout=5)
    except psycopg.Error:
        return False
    try:
        probe.autocommit = True
        with probe.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, IO, TIMING OFF, COSTS OFF) SELECT 1")
            cur.fetchall()
        return True
    except psycopg.Error:
        return False
    finally:
        probe.close()


# Cursor name for the --no-serialize discard path (MOVE FORWARD ALL).
_DISCARD_CURSOR = "_bench_discard_cur"

# Server-side cursor name + batch size for --cursor mode's realistic-app read: the
# result is iterated in fixed-size fetches.  1000 rows/fetch is a typical
# application batch (psycopg's own default is 100; ORMs commonly use ~1000-2000).
_APP_CURSOR = "_bench_cursor_read"
CURSOR_FETCH_ROWS = 1000


def time_discard(conn, sql, serialize, use_cursor=False):
    """Execute the prepared benchmark statement server-side, discard the result,
    and return the client wall-clock time in ms -- with NO executor
    instrumentation (unlike EXPLAIN ANALYZE, whose per-node wrapper inflates
    index-bound scans by several %).

    serialize=True, use_cursor=False : EXECUTE the prepared statement
        (_active_stmt_name) and discard the rows.  Because the statement was prepared
        once under force_generic_plan (prepare_query), each EXECUTE reuses the cached
        generic plan and does NOT re-plan -- so no planning overhead, and in
        particular no get_actual_variable_range index probes that would otherwise
        have us time 2-3 index scans instead of one.  The server still serializes
        every row (via the type output functions) and ships the DataRows over the
        connection (a local Unix socket); libpq drains and buffers them all before
        execute() returns.  We never fetch, so zero Python row objects are built (no
        per-row client noise).
    serialize=True, use_cursor=True : model a real application reading the result
        set -- a server-side cursor iterated in fixed-size fetches
        (CURSOR_FETCH_ROWS rows per round trip), building (and here discarding)
        each row as it arrives.  This overlaps client-side row materialization with
        server execution + prefetch the way an app streaming a large result does,
        rather than the fetchall() shape where libpq buffers the ENTIRE result before
        the first Python row is built (server runs to completion, THEN the client
        decodes -- zero overlap, just a constant decode added to both arms).  A
        server-side cursor cannot DECLARE ... FOR EXECUTE, so this runs the raw *sql*
        and re-plans each call (NOT get_actual_variable_range-clean) -- intended
        here: --cursor models a realistic client, not clean executor timing.
    serialize=False: a NO SCROLL cursor + MOVE FORWARD ALL on the raw *sql* --
        streams every tuple through the executor and discards it without output
        formatting or a tuplestore, approximating EXPLAIN (ANALYZE) without
        SERIALIZE.  NOTE: DECLARE CURSOR re-plans on every call (there is no
        DECLARE ... CURSOR FOR EXECUTE), so unlike the EXECUTE path this re-runs
        planning and is NOT get_actual_variable_range-clean -- a niche diagnostic,
        don't trust it for range-predicate queries.  (use_cursor is irrelevant
        here, since nothing is serialized or returned to the client.)
    """
    if serialize and not use_cursor:
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(f"EXECUTE {_active_stmt_name}", prepare=False)
            return (time.perf_counter() - t0) * 1000.0
    if serialize and use_cursor:
        # Realistic-app read: server-side cursor pulled in fixed-size batches,
        # materializing each row as it arrives (see the docstring).  Time the
        # whole client-perceived read: the cursor execute (DECLARE + first fetch,
        # re-plan included) through the end of iteration.
        s = sql.strip().rstrip(";")
        def _read_all():
            with conn.cursor(name=_APP_CURSOR) as cur:
                cur.itersize = CURSOR_FETCH_ROWS
                t0 = time.perf_counter()
                cur.execute(s)
                for _row in cur:
                    pass
                return (time.perf_counter() - t0) * 1000.0
        # A server-side cursor needs a transaction block; open one only in
        # autocommit, otherwise reuse the in-progress tx (as the no-serialize path).
        if conn.autocommit:
            with conn.transaction():
                return _read_all()
        return _read_all()
    # No-serialize: cursors require a transaction.  Open one only when the
    # connection is in autocommit mode; otherwise reuse the in-progress one.
    sql = sql.strip().rstrip(";")
    own_tx = conn.autocommit
    with conn.cursor() as cur:
        if own_tx:
            cur.execute("BEGIN")
        cur.execute(f"DECLARE {_DISCARD_CURSOR} NO SCROLL CURSOR FOR {sql}")
        t0 = time.perf_counter()
        cur.execute(f"MOVE FORWARD ALL IN {_DISCARD_CURSOR}")
        dt = (time.perf_counter() - t0) * 1000.0
        cur.execute(f"CLOSE {_DISCARD_CURSOR}")
        if own_tx:
            cur.execute("COMMIT")
    return dt


def run_query(conn, query_def, cached_mode, is_master, prefetch_setting, benchmark_cpu,
              serialize=True, direct_io=False, skip_prewarm=False,
              instrument=False, instrument_timing=False, io_supported=False,
              use_cursor=False):
    """
    Run a single query with proper cache preparation and return
    (execution_time_ms, explain_output_or_None).

    Default (instrument=False): time via time_discard() -- EXECUTE the prepared
    statement and discard (serialize) or MOVE FORWARD ALL (--no-serialize) -- with
    no EXPLAIN ANALYZE instrumentation; explain_output is None.  use_cursor=True
    reads the rows back via a server-side cursor to include client-side row decode.

    instrument=True: measure with EXPLAIN (ANALYZE, COSTS OFF, ...) EXECUTE
    (server-reported Execution Time), adding SERIALIZE unless --no-serialize, the
    IO option when io_supported, and per-node TIMING when instrument_timing.  The
    plan text is returned as explain_output.

    skip_prewarm: If True, skip cache preparation entirely.  Useful in
    cached mode where prewarming only needs to happen once before the
    first run of a query, not on every subsequent run.
    """
    # Cache preparation
    if skip_prewarm:
        pass
    elif query_def.get("index_only", False):
        # Pure index-only scan: reads only the index + visibility map, never the
        # heap (the table is all-visible).  Warm the index + heap VM and skip heap
        # eviction and the OS-cache drop entirely -- evicting a large heap the scan
        # never reads is wasted work, and skipping it makes the query behave
        # identically cached and uncached (which is correct for index-only scans).
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
    elif cached_mode:
        # Prewarm everything
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []), include_vm=True)
    else:
        # Uncached: evict heap, prewarm indexes + heap VM, clear OS cache
        evict_relations(conn, query_def.get("evict", []))
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
        if not direct_io:
            clear_os_cache()

    # Set GUCs
    gucs = query_def.get("gucs", {})
    set_gucs(conn, gucs, is_master=is_master, prefetch_setting=prefetch_setting)
    sql = query_def["sql"]

    # Special handling for warmup queries (A3): run twice, untimed, via the
    # same path that will be measured.
    if query_def.get("warmup_query") and not cached_mode:
        if instrument:
            with conn.cursor() as cur:
                cur.execute(f"EXECUTE {_active_stmt_name}")
                cur.execute(f"EXECUTE {_active_stmt_name}")
        else:
            time_discard(conn, sql, serialize, use_cursor)
            time_discard(conn, sql, serialize, use_cursor)

    explain_output = None
    if instrument:
        # EXPLAIN ANALYZE path (opt-in; adds instrumentation overhead).  The IO
        # option is added only on builds whose EXPLAIN accepts it; per-node TIMING
        # is off unless --timing (it roughly doubles the distortion).
        serialize_opt = ", SERIALIZE" if serialize else ""
        io_opt = ", IO" if io_supported else ""
        timing = "ON" if instrument_timing else "OFF"
        explain_sql = (f"EXPLAIN (ANALYZE, COSTS OFF, TIMING {timing}"
                       f"{serialize_opt}{io_opt}) EXECUTE {_active_stmt_name}")
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            result = cur.fetchall()
        explain_output = "\n".join(row[0] for row in result)
        exec_time = extract_execution_time(result)
    else:
        # Default: instrumentation-free COPY / MOVE timing.
        exec_time = time_discard(conn, sql, serialize, use_cursor)

    # Reset query-specific GUCs
    if gucs:
        reset_gucs(conn, gucs)

    if exec_time is None:
        print("Warning: Could not extract execution time from EXPLAIN output")
        return None, explain_output

    return exec_time, explain_output


def measure_query(conn, query_def, args, is_master, prefetch_setting, io_supported,
                  on_patch, force_count=None):
    """Run a query and return (times, explains), choosing the run count dynamically.

    force_count, when given, runs exactly that many times regardless of --runs or
    the dynamic budget.  The patch phase passes the baseline's run count here so
    both sides get the same number of samples (a slower config would otherwise
    get fewer dynamic runs, biasing its min upward).

    With an explicit --runs/--force-runs N: run exactly N times.  Otherwise run
    RUNS_MIN times, then keep running while EITHER budget is unmet (capped at
    RUNS_MAX): the AGGREGATE per-run WALL time -- cache eviction + prewarm + the
    timed query -- is below args.runs_budget, OR the aggregate pure QUERY time
    (the measured per-run times, eviction excluded) is below args.runs_exec_budget.
    The wall budget keeps eviction-heavy queries from running dozens of times; the
    execution budget makes sure such a query still samples enough actual executions
    (e.g. a ~110ms query whose eviction blows the wall budget at RUNS_MIN still gets
    ~5 runs).  Per-run query times are the benchmark result (printed unless
    --terse); wall time only sizes the loop.

    Cached mode and index_only queries prewarm once (before the first run); plain
    uncached queries re-evict + re-prewarm on every run.
    """
    times, explains, walls = [], [], []
    index_only = query_def.get("index_only", False)
    n_done = 0

    def one():
        nonlocal n_done
        skip_prewarm = n_done > 0 and (args.cached or index_only)
        t0 = time.perf_counter()
        t, e = run_query(
            conn, query_def, args.cached, is_master=is_master,
            prefetch_setting=prefetch_setting, benchmark_cpu=args.benchmark_cpu,
            serialize=not args.no_serialize,
            direct_io=server_direct_io(args, on_patch),
            skip_prewarm=skip_prewarm, instrument=args.instrument,
            instrument_timing=args.instrument_timing,
            io_supported=io_supported, use_cursor=args.cursor)
        wall = (time.perf_counter() - t0) * 1000.0   # full per-run wall: setup + query
        n_done += 1
        if t is not None:
            times.append(t)
            explains.append(e)
            walls.append(wall)
            if not args.terse:
                print(f"  Run {len(times)}: {t:.3f} ms")
        return t

    if force_count is not None:
        for _ in range(force_count):                # match the baseline's count
            one()
        return times, explains

    if args.runs is not None:
        for _ in range(args.runs):                 # exact forced count
            one()
        return times, explains

    for _ in range(RUNS_MIN):                       # dynamic floor (always >= 3)
        one()
    # Extend while EITHER budget is still unmet (capped at RUNS_MAX): the wall
    # budget (eviction + prewarm + query) reins in eviction-heavy queries, while
    # the execution-time budget (pure query time, eviction excluded) makes sure
    # even those still sample enough actual executions instead of stopping at the
    # RUNS_MIN floor.
    while n_done < RUNS_MAX and walls and (sum(walls) < args.runs_budget
                                           or sum(times) < args.runs_exec_budget):
        one()
    return times, explains


def load_most_recent_master_results(mode_name, needed_queries=None):
    """Load master results from the most recent benchmark JSON file for a given mode.

    Searches result files from newest to oldest.  When *needed_queries* is
    given, skips files that don't contain master results for at least one of
    those queries; this avoids picking up a tiny single-query run when a
    larger earlier run has the data we need.

    Args:
        mode_name: The benchmark mode to match (e.g. "benchmark_cached",
                   "worker_regress_uncached").  Files are matched by their
                   "{mode_name}_" prefix.
        needed_queries: Optional list of query IDs that the caller wants
                        master results for.  If provided, the first file
                        (newest) that contains at least one of them is used.

    Returns:
        tuple: (old_results dict, json_file path) or (None, None) if not found
    """
    if not os.path.exists(OUTPUT_DIR):
        return None, None

    # Find JSON files matching this mode's naming convention
    prefix = f"{mode_name}_"
    json_files = []
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith(prefix) and f.endswith(".json"):
            json_files.append(os.path.join(OUTPUT_DIR, f))

    if not json_files:
        return None, None

    # Sort by modification time (most recent first)
    json_files.sort(key=os.path.getmtime, reverse=True)

    for json_file in json_files[:500]:
        try:
            with open(json_file, "r") as f:
                old_results = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load {json_file}: {e}")
            continue

        # If caller needs specific queries, check that this file has at least one
        if needed_queries:
            old_queries = old_results.get("queries", {})
            has_any = any(
                qid in old_queries and old_queries[qid].get("master", {}).get("min") is not None
                for qid in needed_queries
            )
            if not has_any:
                continue

        return old_results, json_file

    return None, None


def print_ratio_rankings(all_ratios, args, baseline_name, baseline_label, scope=""):
    """Print NEUTRAL / IMPROVEMENTS / REGRESSIONS rankings and the OVERALL
    SUMMARY (totals + geometric mean of ratios) for a list of ratio entries.

    Each entry is a dict with query_id, name, config, ratio, master_ms and
    patch_ms (as built in run_generic_benchmark).  Factored out so the same
    rankings print per-suite and, when --queries spans suites, once more over
    the pooled set of every query run.

    scope: optional label prefixed onto section headers (e.g. "ALL SUITES — ")
           to distinguish a combined cross-suite summary from a per-suite one.
    """
    stat_label = get_stat_label()
    BOLD = "\033[1m"
    RESET = "\033[0m"

    # Classify each entry (improvement / regression / neutral).  "neutral" folds
    # in noise-level absolute differences regardless of ratio (see classify_ratio),
    # so e.g. 0.133ms vs 0.134ms is neutral, not a regression.
    for e in all_ratios:
        e["category"] = classify_ratio(e["master_ms"], e["patch_ms"], args.noise_abs_ms)
    neutral = sorted([e for e in all_ratios if e["category"] == "neutral"], key=lambda x: x["ratio"])
    improvements = sorted([e for e in all_ratios if e["category"] == "improvement"], key=lambda x: x["ratio"])
    regressions = sorted([e for e in all_ratios if e["category"] == "regression"], key=lambda x: x["ratio"], reverse=True)

    def _print_section(header, entries):
        print(f"\n{'=' * 60}")
        print(header)
        print(f"{'=' * 60}")
        if entries:
            for rank, entry in enumerate(entries, 1):
                print(f"  #{rank}  {entry['query_id']} ({entry['config']}): {entry['name']}")
                print(f"       {BOLD}{entry['ratio']:.3f}x{RESET} - {baseline_name} ({stat_label}): "
                      f"{entry['master_ms']:.3f} ms [{entry.get('master_runs', '?')} runs], "
                      f"patch ({stat_label}): {entry['patch_ms']:.3f} ms [{entry.get('patch_runs', '?')} runs]")
        else:
            print("  (none)")

    _print_section(f"{scope}NEUTRAL vs {baseline_label} (using {stat_label}) [{len(neutral)} total]", neutral)
    _print_section(f"{scope}IMPROVEMENTS vs {baseline_label} (using {stat_label}) [{len(improvements)} total]", improvements)
    _print_section(f"{scope}REGRESSIONS vs {baseline_label} (using {stat_label}) [{len(regressions)} total]", regressions)

    # Print overall summary comparing patch vs master
    # Determine which config to summarize based on benchmark settings
    if args.prefetch_disabled:
        summary_config = "prefetch=off"
        summary_label = f"patch + no prefetch vs {baseline_name}"
    else:
        summary_config = "prefetch=on"
        summary_label = f"patch + prefetch vs {baseline_name}"

    # Filter ratios for the applicable config
    config_ratios = [e for e in all_ratios if e["config"] == summary_config]

    if config_ratios:
        # Two views of the same data, printed back to back: one INCLUDING neutral
        # (noise-level) queries in the totals / ratio-of-totals / geomean /
        # best-worst, and one EXCLUDING them.  Neutral results have often-extreme
        # ratios that distort the geomean, so the net-of-neutral view is the
        # headline -- it is printed LAST for prominence.  Within each block the
        # "Number of queries", totals, ratio and geomean all describe the same
        # population (the block's `entries`), so the block is self-consistent on
        # its own.  improved/regressed are view-independent (a neutral query is
        # neither), so they sum to len(entries) in each view; only the neutral
        # count and the query total differ between the two.
        scored = [e for e in config_ratios if e["category"] != "neutral"]
        n_improved = sum(1 for e in config_ratios if e["category"] == "improvement")
        n_regressed = sum(1 for e in config_ratios if e["category"] == "regression")
        n_neutral = sum(1 for e in config_ratios if e["category"] == "neutral")

        def _print_overall(neutral_note, entries, excluded=0):
            label = f"{summary_label} ({neutral_note})" if neutral_note else summary_label
            header = f"{scope}OVERALL SUMMARY: {label}"
            bar = "=" * len(header)
            print(f"\n{bar}")
            print(header)
            print(bar)
            # Count the population the totals/ratio/geomean below are computed over.
            count_note = (f"   ({len(config_ratios)} total, {excluded} neutral excluded)"
                          if excluded else "")
            print(f"  Number of queries:             {len(entries)}{count_note}")
            if entries:
                total_master_ms = sum(e["master_ms"] for e in entries)
                total_patch_ms = sum(e["patch_ms"] for e in entries)
                total_ratio = total_patch_ms / total_master_ms
                geomean_ratio = math.exp(sum(math.log(e["ratio"]) for e in entries) / len(entries))
                best_ratio = min(e["ratio"] for e in entries)
                worst_ratio = max(e["ratio"] for e in entries)
                master_label = f"Total exec time ({baseline_name}):"
                print(f"  {master_label:<26}{total_master_ms:10.3f} ms")
                print(f"  {'Total exec time (patch):':<26}{total_patch_ms:10.3f} ms")
                print(f"  Ratio of exec time totals:     {BOLD}{total_ratio:.3f}x{RESET}")
                print(f"  Geometric mean of ratios:      {BOLD}{geomean_ratio:.3f}x{RESET}")
            print(f"  Queries improved:              {n_improved}")
            print(f"  Queries regressed:             {n_regressed}")
            if not excluded:
                print(f"  Queries neutral:               {n_neutral}")
            if entries:
                print(f"  Best case ratio:               {best_ratio:.3f}x")
                print(f"  Worst case ratio:              {worst_ratio:.3f}x")

        # With neutral queries present, print both views (included, then the
        # net-of-neutral headline last).  With none, a single unqualified block --
        # the "excluded" view would just duplicate it.
        if n_neutral:
            _print_overall("neutral included", config_ratios)
            _print_overall("neutral excluded", scored, excluded=n_neutral)
        else:
            _print_overall("", config_ratios)


def _arm_tmpfs_cleanup(tmpfs_mount):
    """Ensure the tmpfs hugepage mount is torn down even if the run aborts
    (e.g. sys.exit on a failed data check) -- it would otherwise leak.

    Registers an atexit hook and returns a callable for the normal end-of-run
    cleanup; the mount is unmounted at most once, whichever fires first.
    """
    state = {"done": False}

    def _cleanup():
        if tmpfs_mount and not state["done"]:
            state["done"] = True
            try:
                cleanup_tmpfs(tmpfs_mount)
            except Exception as e:
                print(f"Warning: tmpfs cleanup failed: {e}")

    if tmpfs_mount:
        atexit.register(_cleanup)
    return _cleanup


def run_generic_benchmark(args, queries_dict, mode_name, title,
                          verify_func=None, load_func=None,
                          sync_stats=False, tables=None,
                          expect_all_visible=True, query_ids=None):
    """Run a benchmark suite.

    This is the unified benchmark runner used by all benchmark modes
    (see BENCHMARK_SUITES registry). It handles tmpfs setup,
    data verification/loading, master runs, patch runs, results summary,
    file saving, and rankings.

    Args:
        args: Parsed command-line arguments.
        queries_dict: OrderedDict of query definitions (a suite's "queries" mapping).
        mode_name: Short mode identifier for result files (e.g. "benchmark", "worker_regress").
        title: Display title for the benchmark (e.g. "Prefetch Benchmark").
        verify_func: Function(conn_details) -> bool to verify data exists.
                     For the default benchmark with sync_stats=True, this takes
                     (conn_details, skip_load) instead.
        load_func: Function(conn_details) to load data.
        sync_stats: If True, extract optimizer stats from master and restore to patch
                    (only needed for the default benchmark where tables are shared).
        tables: List of table names belonging to this suite (used to verify
                all-visible status after loading).
        expect_all_visible: If True (default), verify all pages are all-visible
                            after loading and VACUUM FREEZE if not.
        query_ids: Explicit list of query ids to run from queries_dict. When
                   provided (e.g. by the cross-suite --queries resolver in
                   main()), it overrides args.queries parsing; the ids are
                   assumed already validated against this suite's dict.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Resolve baseline and testbranch configuration
    baseline_name = getattr(args, "baseline", "master")
    baseline_label = baseline_name.upper()
    baseline_bin_orig, baseline_data_dir, baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    testbranch_name = getattr(args, "testbranch", "patch")
    test_bin_orig, test_data_dir, test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]

    # master shares the patch build's data directory (see MASTER_DATA_DIR).
    # One set of files means one catalog: there are no optimizer statistics to
    # copy from the baseline to the testbranch, and the data is verified once.
    shared_data_dir = (os.path.realpath(baseline_data_dir)
                       == os.path.realpath(test_data_dir))
    if shared_data_dir:
        sync_stats = False

    # Sampled up front so the value describes the run itself.
    nvme_awake = nvme_kept_awake()
    note_nvme_power_state(args, nvme_awake)

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = baseline_bin_orig
    patch_bin = test_bin_orig
    cleanup_tmpfs_now = lambda: None   # replaced once tmpfs exists; armed vs aborts

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        cleanup_tmpfs_now = _arm_tmpfs_cleanup(tmpfs_mount)
        master_bin = copy_binaries_to_tmpfs(baseline_bin_orig, tmpfs_mount, "baseline")
        patch_bin = copy_binaries_to_tmpfs(test_bin_orig, tmpfs_mount, "testbranch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  baseline ({baseline_name}): {master_bin}")
        print(f"  testbranch ({testbranch_name}): {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    # Parse query selection
    if query_ids is not None:
        # Explicit per-suite selection resolved by the caller (cross-suite
        # --queries); already validated against this suite's dict.
        selected_queries = list(query_ids)
    elif args.queries:
        selected_queries = [q.strip().upper() for q in args.queries.split(",")]
        for q in selected_queries:
            if q not in queries_dict:
                print(f"Error: Unknown query '{q}'. Available: {', '.join(queries_dict.keys())}")
                sys.exit(1)
    else:
        selected_queries = list(queries_dict.keys())

    # Handle --old-master-results: load previous results and filter queries
    old_master_data = None
    old_master_file = None
    old_results = None
    if args.old_master_results:
        old_results, old_master_file = load_most_recent_master_results(mode_name, selected_queries)
        if old_results is None:
            print("No previous master results found, will run master fresh")
            args.old_master_results = False
        else:
            # Extract master data from old results
            old_master_data = {}
            old_queries = old_results.get("queries", {})
            for qid in list(selected_queries):
                if qid in old_queries and old_queries[qid].get("master", {}).get("min") is not None:
                    old_master_data[qid] = old_queries[qid]["master"]
                else:
                    print(f"Warning: Query {qid} has no master results in previous run, skipping")
                    selected_queries.remove(qid)

            if not selected_queries:
                print("No queries with existing master results, will run master fresh")
                args.old_master_results = False
                old_results = None
                old_master_data = None
                old_master_file = None
                # Restore the original query selection (don't expand to all queries)
                if query_ids is not None:
                    selected_queries = list(query_ids)
                elif args.queries:
                    selected_queries = [q.strip().upper() for q in args.queries.split(",")]
                else:
                    selected_queries = list(queries_dict.keys())
            else:
                print(f"\nUsing master results from: {old_master_file}")
                print(f"  Original master hash: {old_results.get('master_hash', 'unknown')}")
                print(f"  Queries with results: {', '.join(selected_queries)}")

    print(f"\n{'=' * 60}")
    print(title)
    print(f"{'=' * 60}")
    mode_word = ("\033[32m💵  cached\033[0m" if args.cached
                 else "\033[34m💾  uncached\033[0m")
    print(f"Mode: {mode_word}")
    print(f"Queries: {', '.join(selected_queries)}")
    if args.runs is not None:
        print(f"Runs per query: {args.runs} (forced)")
    else:
        print(f"Runs per query: dynamic (min {RUNS_MIN}, up to {RUNS_MAX}, "
              f"wall budget {args.runs_budget:.0f} ms or exec budget "
              f"{args.runs_exec_budget:.0f} ms)")
    print(f"{'=' * 60}\n")

    # Get git hashes
    master_hash = get_git_hash(baseline_source_dir)
    patch_hash = get_git_hash(test_source_dir)
    if not args.terse:
        print(f"Baseline ({baseline_name}) git hash: {master_hash}")
        print(f"Testbranch ({testbranch_name}) git hash: {patch_hash}")
        if shared_data_dir:
            print(f"Data directory: {test_data_dir} (shared by both builds)")
        print_io_settings(args)

    # Verify/load data on each server (one at a time due to memory constraints)
    master_stats = None
    master_version = None
    patch_version = None
    if not args.skip_load and verify_func and load_func:
        # verify_func takes only conn_details for every suite.  sync_stats (the
        # default benchmark) additionally copies optimizer statistics from the
        # baseline to the testbranch so plans match; it no longer affects how data
        # is verified or loaded.

        # Whether the baseline server has to be started at all: to verify (and
        # if need be load) the data, and under sync_stats to extract the
        # statistics.  --old-master-results never measures the baseline, so it
        # is started only when nothing else can do the job: saved statistics
        # cover the sync_stats case, and with a shared data directory the
        # testbranch verifies the very same files.
        verify_on_baseline = True
        if args.old_master_results and old_results:
            if sync_stats:
                # Load saved statistics from old results instead of starting master
                master_stats = old_results.get("master_stats")
                if master_stats:
                    print(f"\nUsing saved master statistics from previous results")
                    verify_on_baseline = False
                else:
                    print("Warning: Old results have no saved master_stats, starting master to extract them")
            elif shared_data_dir:
                verify_on_baseline = False

        if verify_on_baseline:
            print(f"\n--- Verifying data on baseline ({baseline_name}) ---")
            start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
            if not verify_func(baseline_conn):
                print(f"Loading data on {baseline_name}...")
                load_func(baseline_conn)
                if expect_all_visible and tables:
                    ensure_all_visible_after_load(baseline_conn, tables)
            if sync_stats:
                master_stats = extract_statistics(baseline_conn)
            master_version = get_pg_version(baseline_conn)
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)
        else:
            master_version = old_results.get("master_version")

        if shared_data_dir and verify_on_baseline:
            print(f"\n--- Testbranch ({testbranch_name}) shares the baseline's "
                  f"data directory: verified above ---")
        else:
            print(f"\n--- Verifying data on testbranch ({testbranch_name}) ---")
            start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
            if not verify_func(test_conn):
                print(f"Loading data on {testbranch_name}...")
                load_func(test_conn)
                if expect_all_visible and tables:
                    ensure_all_visible_after_load(test_conn, tables)
            if sync_stats:
                restore_statistics(test_conn, master_stats)
            patch_version = get_pg_version(test_conn)
            stop_server(patch_bin, test_data_dir)
            time.sleep(2)

    # Results storage
    # Use old master hash if using old master results
    effective_master_hash = master_hash
    if args.old_master_results and old_results:
        effective_master_hash = old_results.get("master_hash", master_hash)
    results = {
        "timestamp": datetime.now().isoformat(),
        "baseline": baseline_name,
        "testbranch": testbranch_name,
        "master_hash": effective_master_hash,
        "patch_hash": patch_hash,
        "master_version": master_version,
        "patch_version": patch_version,
        "mode": mode_name,
        "runs": args.runs if args.runs is not None else "dynamic",
        "runs_budget": args.runs_budget,
        "runs_exec_budget": args.runs_exec_budget,
        "noise_abs_ms": args.noise_abs_ms,
        "io_settings": {
            "effective_io_concurrency": args.effective_io_concurrency,
            "io_combine_limit": args.io_combine_limit,
            "io_max_combine_limit": 128,
            "io_max_concurrency": IO_MAX_CONCURRENCY,
            "io_max_workers": args.io_max_workers,
            "io_min_workers": args.io_min_workers,
            "io_worker_launch_interval": args.io_worker_launch_interval,
            "io_method": args.io_method,
            "direct_io": getattr(args, "direct_io", False),
            "patch_direct": getattr(args, "patch_direct", False),
        },
        "queries": {},
        "master_results_from": old_master_file if args.old_master_results else None,
        "master_stats": master_stats,
        "shared_data_dir": shared_data_dir,
        "nvme_awake": nvme_awake,
    }

    # Initialize query results structure
    for query_id in selected_queries:
        query_def = queries_dict[query_id]
        # Use old master data if available, otherwise initialize empty
        if old_master_data and query_id in old_master_data:
            master_result = old_master_data[query_id].copy()
        else:
            master_result = {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None}
        results["queries"][query_id] = {
            "name": query_def["name"],
            "master": master_result,
            "patch_off": {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_on": {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None},
        }

    # Run all queries on master (skip if using old master results)
    if args.old_master_results:
        print(f"\n{'=' * 60}")
        print(f"SKIPPING {baseline_label} (using previous results)")
        print(f"{'=' * 60}")
        master_start_time = 0
        master_end_time = 0
        # Get master version from old results if available
        if old_results and old_results.get("master_version"):
            master_version = old_results["master_version"]
    else:
        print(f"\n{'=' * 60}")
        print(f"Running all queries on BASELINE ({baseline_name})")
        print(f"{'=' * 60}")
        master_start_time = time.time()
        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
        try:
            master_conn = psycopg.connect(**baseline_conn)
            pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            if master_version is None:
                master_version = get_pg_version(baseline_conn)
            with master_conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

            master_io = explain_supports_io(baseline_conn) if args.instrument else False
            if args.instrument and not master_io:
                print(f"Note: {baseline_name} EXPLAIN has no IO option; "
                      f"running it without IO instrumentation")

            for query_id in selected_queries:
                query_def = queries_dict[query_id]
                print(f"\n{query_id}: {query_def['name']}...")
                prepare_query(master_conn, query_def["sql"],
                              gucs=query_def.get("gucs", {}), is_master=True,
                              stmt_name=bench_stmt_name(query_id))
                times, explains = measure_query(
                    master_conn, query_def, args, is_master=True,
                    prefetch_setting=None, io_supported=master_io,
                    on_patch=False)
                results["queries"][query_id]["master"]["times"].extend(times)
                results["queries"][query_id]["master"]["explains"].extend(explains)
                deallocate_query(master_conn)

            master_conn.close()
        finally:
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)
        master_end_time = time.time()

    # Run all queries on testbranch (both prefetch=off and prefetch=on, or plain if no prefetch GUC)
    print(f"\n{'=' * 60}")
    print(f"Running all queries on TESTBRANCH ({testbranch_name})")
    print(f"{'=' * 60}")
    patch_start_time = time.time()
    start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
    try:
        patch_conn = psycopg.connect(**test_conn)
        pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
        if patch_version is None:
            patch_version = get_pg_version(test_conn)
        with patch_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        # Tolerate a testbranch build that lacks the prefetch GUC (warns + treats
        # it as a baseline).  Persists to the regression re-confirm phase below.
        test_has_prefetch = verify_test_has_prefetch(
            patch_conn, test_has_prefetch, testbranch_name)

        patch_io = explain_supports_io(test_conn) if args.instrument else False
        if args.instrument and not patch_io:
            print(f"Note: {testbranch_name} EXPLAIN has no IO option; "
                  f"running it without IO instrumentation")

        for query_id in selected_queries:
            query_def = queries_dict[query_id]
            # When testbranch lacks the prefetch GUC, treat it like a baseline
            test_is_master = not test_has_prefetch
            # Match the baseline's run count so the slower side never gets fewer
            # dynamic runs (which would bias its min upward).  None -> dynamic.
            master_n = len(results["queries"][query_id]["master"]["times"])
            patch_count = master_n if master_n > 0 else None
            prepare_query(patch_conn, query_def["sql"],
                          gucs=query_def.get("gucs", {}), is_master=test_is_master,
                          stmt_name=bench_stmt_name(query_id))

            # Run with prefetch OFF (skip if --prefetch-only or testbranch has no prefetch GUC)
            if not args.prefetch_only and test_has_prefetch:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off)...")
                times, explains = measure_query(
                    patch_conn, query_def, args, is_master=False,
                    prefetch_setting="off", io_supported=patch_io,
                    on_patch=True, force_count=patch_count)
                results["queries"][query_id]["patch_off"]["times"].extend(times)
                results["queries"][query_id]["patch_off"]["explains"].extend(explains)

            # Run with prefetch ON (skip if --prefetch-disabled)
            # When testbranch has no prefetch GUC, run queries plain (results stored in patch_on)
            if not args.prefetch_disabled:
                if test_has_prefetch:
                    print(f"\n{query_id}: {query_def['name']} (prefetch=on)...")
                    pf_setting = "on"
                else:
                    print(f"\n{query_id}: {query_def['name']}...")
                    pf_setting = None
                times, explains = measure_query(
                    patch_conn, query_def, args, is_master=test_is_master,
                    prefetch_setting=pf_setting, io_supported=patch_io,
                    on_patch=True, force_count=patch_count)
                results["queries"][query_id]["patch_on"]["times"].extend(times)
                results["queries"][query_id]["patch_on"]["explains"].extend(explains)

            deallocate_query(patch_conn)

        patch_conn.close()
    finally:
        stop_server(patch_bin, test_data_dir)
        time.sleep(2)
    patch_end_time = time.time()

    # --- Auto-confirmation pass ---------------------------------------------
    # The baseline and patch phases run minutes apart, so a query can be flagged
    # as a regression just for catching a sustained transient that min() (which
    # only filters brief blips) cannot remove.  Re-run only the apparent
    # REGRESSIONS (patch slower than master) on both servers and POOL the new
    # samples into the existing times: min() over the decorrelated re-measurement
    # washes out a one-off transient on either side, while a genuine regression
    # survives.  Improvements (patch faster) are the expected direction for a
    # prefetch win, so they are not re-confirmed -- only the surprising direction
    # is worth the cost.  This deliberately just adds samples (gentle) rather
    # than discarding a result the moment one retry clears, the way
    # --stress-test does.  Skipped with --no-confirm, with an exact --runs count,
    # and with --old-master-results (no live baseline to re-measure).
    do_confirm = (not args.no_confirm and not args.old_master_results
                  and args.runs is None)
    flagged = []
    if do_confirm:
        for query_id in selected_queries:
            qr = results["queries"][query_id]
            m = get_representative(qr["master"]["times"])
            if not m:
                continue
            for cfg in ("patch_off", "patch_on"):
                p = get_representative(qr[cfg]["times"])
                if p and classify_ratio(m, p, args.noise_abs_ms) == "regression":
                    flagged.append(query_id)
                    break

    if flagged:
        print(f"\n{'=' * 60}")
        print("AUTO-CONFIRMATION PASS")
        print(f"{'=' * 60}")
        print(f"Re-measuring {len(flagged)} apparent "
              f"{'regression' if len(flagged) == 1 else 'regressions'} "
              f"(patch slower than {baseline_name}) to filter "
              f"transients: {', '.join(flagged)}")

        # Snapshot pre-confirmation representatives for the before/after report.
        before = {}
        for query_id in flagged:
            qr = results["queries"][query_id]
            before[query_id] = {
                cfg: get_representative(qr[cfg]["times"])
                for cfg in ("master", "patch_off", "patch_on")
            }

        # Re-measure on the baseline; record each query's new sample count so the
        # testbranch re-measure can match it (keeps the pooled counts symmetric).
        confirm_n = {}
        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
        try:
            mconn = psycopg.connect(**baseline_conn)
            pin_backend(mconn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            with mconn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
            m_io = explain_supports_io(baseline_conn) if args.instrument else False
            for query_id in flagged:
                qd = queries_dict[query_id]
                print(f"\n{query_id}: {qd['name']} (confirm {baseline_name})...")
                prepare_query(mconn, qd["sql"], gucs=qd.get("gucs", {}),
                              is_master=True, stmt_name=bench_stmt_name(query_id))
                times, explains = measure_query(
                    mconn, qd, args, is_master=True, prefetch_setting=None,
                    io_supported=m_io, on_patch=False)
                confirm_n[query_id] = len(times)
                results["queries"][query_id]["master"]["times"].extend(times)
                results["queries"][query_id]["master"]["explains"].extend(explains)
                deallocate_query(mconn)
            mconn.close()
        finally:
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)

        # Re-measure on the testbranch, matching the baseline's confirm count.
        start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
        try:
            pconn = psycopg.connect(**test_conn)
            pin_backend(pconn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            with pconn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
            p_io = explain_supports_io(test_conn) if args.instrument else False
            test_is_master = not test_has_prefetch
            for query_id in flagged:
                qd = queries_dict[query_id]
                fc = confirm_n.get(query_id) or None
                prepare_query(pconn, qd["sql"], gucs=qd.get("gucs", {}),
                              is_master=test_is_master, stmt_name=bench_stmt_name(query_id))
                if not args.prefetch_only and test_has_prefetch:
                    print(f"\n{query_id}: {qd['name']} (confirm prefetch=off)...")
                    times, explains = measure_query(
                        pconn, qd, args, is_master=False, prefetch_setting="off",
                        io_supported=p_io, on_patch=True, force_count=fc)
                    results["queries"][query_id]["patch_off"]["times"].extend(times)
                    results["queries"][query_id]["patch_off"]["explains"].extend(explains)
                if not args.prefetch_disabled:
                    pf = "on" if test_has_prefetch else None
                    print(f"\n{query_id}: {qd['name']} (confirm prefetch=on)...")
                    times, explains = measure_query(
                        pconn, qd, args, is_master=test_is_master, prefetch_setting=pf,
                        io_supported=p_io, on_patch=True, force_count=fc)
                    results["queries"][query_id]["patch_on"]["times"].extend(times)
                    results["queries"][query_id]["patch_on"]["explains"].extend(explains)
                deallocate_query(pconn)
            pconn.close()
        finally:
            stop_server(patch_bin, test_data_dir)
            time.sleep(2)

        # Report before -> after (pooled min) and whether the flag held.
        print(f"\n{'=' * 60}")
        print("CONFIRMATION RESULTS (pooled min, before -> after)")
        print(f"{'=' * 60}")
        for query_id in flagged:
            qr = results["queries"][query_id]
            m_before = before[query_id]["master"]
            m_after = get_representative(qr["master"]["times"])
            for cfg, label in (("patch_off", "prefetch=off"), ("patch_on", "prefetch=on")):
                p_before = before[query_id][cfg]
                if not p_before:
                    continue
                p_after = get_representative(qr[cfg]["times"])
                cat_before = classify_ratio(m_before, p_before, args.noise_abs_ms)
                cat_after = classify_ratio(m_after, p_after, args.noise_abs_ms)
                rb = p_before / m_before if m_before else float("nan")
                ra = p_after / m_after if m_after else float("nan")
                verdict = (f"{cat_before} confirmed" if cat_after == cat_before
                           else f"{cat_before} -> {cat_after}")
                print(f"  {query_id} ({label}): {rb:.3f}x -> {ra:.3f}x  [{verdict}]")

    # Cleanup tmpfs if it was created (also armed via atexit against aborts)
    cleanup_tmpfs_now()

    # Calculate statistics
    for query_id in selected_queries:
        query_results = results["queries"][query_id]
        for config in ["master", "patch_off", "patch_on"]:
            times = query_results[config]["times"]
            if times:
                query_results[config]["avg"] = mean(times)
                query_results[config]["min"] = min(times)
                query_results[config]["median"] = median(times)
                query_results[config]["max"] = max(times)
            # Select the explain output from the run closest to the representative value
            explains = query_results[config].get("explains", [])
            if times and explains:
                rep = get_representative(times)
                best_idx = min(range(len(times)), key=lambda i: abs(times[i] - rep))
                query_results[config]["explain"] = explains[best_idx]
            # Drop the explains list before saving to JSON (it's bulky and redundant)
            query_results[config].pop("explains", None)

    # Print per-query detail (suppressed in terse mode)
    if not args.terse:
        print(f"\n{'=' * 60}")
        print("RESULTS SUMMARY")
        print(f"{'=' * 60}")

        for query_id in selected_queries:
            query_results = results["queries"][query_id]

            stat_label = get_stat_label()
            master_rep = get_representative(query_results["master"]["times"])
            patch_off_rep = get_representative(query_results["patch_off"]["times"])
            patch_on_rep = get_representative(query_results["patch_on"]["times"])

            # ANSI bold escape codes
            BOLD = "\033[1m"
            RESET = "\033[0m"

            print(f"\n{BOLD}{query_id}: {query_results['name']}{RESET}")
            if master_rep:
                print(f"  {baseline_name} ({stat_label}):               {master_rep:10.3f} ms "
                      f"(avg={query_results['master']['avg']:.3f}, max={query_results['master']['max']:.3f})")
            if patch_off_rep and master_rep:
                ratio_off = patch_off_rep / master_rep
                print(f"  patch (prefetch=off) ({stat_label}): {patch_off_rep:10.3f} ms "
                      f"(avg={query_results['patch_off']['avg']:.3f}, max={query_results['patch_off']['max']:.3f}) "
                      f"[{BOLD}{ratio_off:.3f}x{RESET} vs {baseline_name}]")
            if patch_on_rep and master_rep:
                ratio_on = patch_on_rep / master_rep
                print(f"  patch (prefetch=on) ({stat_label}):  {patch_on_rep:10.3f} ms "
                      f"(avg={query_results['patch_on']['avg']:.3f}, max={query_results['patch_on']['max']:.3f}) "
                      f"[{BOLD}{ratio_on:.3f}x{RESET} vs {baseline_name}]")

            # Print query text and EXPLAIN ANALYZE outputs
            # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
            print()
            query_sql = queries_dict[query_id]["sql"].strip()
            print("  Query:")
            for line in query_sql.split('\n'):
                print(f"    {line.strip()}")
            print()
            if query_results["master"]["explain"]:
                print(f"  {baseline_name} EXPLAIN ANALYZE:")
                for line in query_results["master"]["explain"].split('\n'):
                    print(f"    {line}")
            if args.prefetch_disabled:
                if query_results["patch_off"]["explain"]:
                    print()
                    print("  patch (prefetch=off) EXPLAIN ANALYZE:")
                    for line in query_results["patch_off"]["explain"].split('\n'):
                        print(f"    {line}")
            else:
                if query_results["patch_on"]["explain"]:
                    print()
                    print("  patch (prefetch=on) EXPLAIN ANALYZE:")
                    for line in query_results["patch_on"]["explain"].split('\n'):
                        print(f"    {line}")

    # Save results
    # Update versions in case they were fetched during benchmark run (--skip-load)
    results["master_version"] = master_version
    results["patch_version"] = patch_version

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_file = os.path.join(OUTPUT_DIR, f"{mode_name}_{timestamp}.json")
    txt_file = os.path.join(OUTPUT_DIR, f"{mode_name}_{timestamp}.txt")

    # Save JSON
    with open(json_file, "w") as f:
        json.dump(results, f, indent=2)
    if not args.terse:
        print(f"\nResults saved to: {json_file}")

    # Save human-readable text
    with open(txt_file, "w") as f:
        f.write("=" * 70 + "\n")
        f.write(f"{title} Results\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Timestamp: {results['timestamp']}\n")
        f.write(f"{baseline_label} git hash: {results['master_hash']}\n")
        f.write(f"Patch git hash: {results['patch_hash']}\n")
        f.write(f"Mode: {results['mode']}\n")
        f.write(f"Runs per query: {results['runs']}\n\n")

        stat_label = get_stat_label()
        for query_id, qr in results["queries"].items():
            f.write(f"\n{query_id}: {qr['name']}\n")
            f.write("-" * 50 + "\n")

            master_rep = get_representative(qr["master"]["times"])
            patch_off_rep = get_representative(qr["patch_off"]["times"])
            patch_on_rep = get_representative(qr["patch_on"]["times"])

            if master_rep:
                f.write(f"  {baseline_name} ({stat_label}):               {master_rep:10.3f} ms "
                        f"(avg={qr['master']['avg']:.3f}, max={qr['master']['max']:.3f})\n")
            if patch_off_rep and master_rep:
                ratio_off = patch_off_rep / master_rep
                f.write(f"  patch (prefetch=off) ({stat_label}): {patch_off_rep:10.3f} ms "
                        f"(avg={qr['patch_off']['avg']:.3f}, max={qr['patch_off']['max']:.3f}) "
                        f"[{ratio_off:.3f}x vs {baseline_name}]\n")
            if patch_on_rep and master_rep:
                ratio_on = patch_on_rep / master_rep
                f.write(f"  patch (prefetch=on) ({stat_label}):  {patch_on_rep:10.3f} ms "
                        f"(avg={qr['patch_on']['avg']:.3f}, max={qr['patch_on']['max']:.3f}) "
                        f"[{ratio_on:.3f}x vs {baseline_name}]\n")

            # Write query text and EXPLAIN ANALYZE outputs
            # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
            f.write("\n")
            query_sql = queries_dict[query_id]["sql"].strip()
            f.write("  Query:\n")
            for line in query_sql.split('\n'):
                f.write(f"    {line.strip()}\n")
            f.write("\n")
            if qr["master"]["explain"]:
                f.write(f"  {baseline_name} EXPLAIN ANALYZE:\n")
                for line in qr["master"]["explain"].split('\n'):
                    f.write(f"    {line}\n")
            if args.prefetch_disabled:
                if qr["patch_off"]["explain"]:
                    f.write("\n")
                    f.write("  patch (prefetch=off) EXPLAIN ANALYZE:\n")
                    for line in qr["patch_off"]["explain"].split('\n'):
                        f.write(f"    {line}\n")
            else:
                if qr["patch_on"]["explain"]:
                    f.write("\n")
                    f.write("  patch (prefetch=on) EXPLAIN ANALYZE:\n")
                    for line in qr["patch_on"]["explain"].split('\n'):
                        f.write(f"    {line}\n")

    if not args.terse:
        print(f"Results saved to: {txt_file}")

    # Update latest symlink
    latest_link = os.path.join(OUTPUT_DIR, "latest.txt")
    if os.path.exists(latest_link):
        os.remove(latest_link)
    os.symlink(os.path.basename(txt_file), latest_link)

    # Print total run times
    master_duration = master_end_time - master_start_time
    patch_duration = patch_end_time - patch_start_time
    total_duration = master_duration + patch_duration

    if not args.terse:
        print(f"\n{'=' * 60}")
        print("BENCHMARK RUN TIMES (excluding data loading)")
        print(f"{'=' * 60}")
        print(f"  {baseline_label + ':':11s}{master_duration:10.1f} seconds ({_format_duration(master_duration)})")
        print(f"  Patch:   {patch_duration:10.1f} seconds ({_format_duration(patch_duration)})")
        print(f"  Total:   {total_duration:10.1f} seconds ({_format_duration(total_duration)})")

    # Collect all patch runs with their ratios vs master (using min or median as representative)
    # Each patch configuration (prefetch=off, prefetch=on) is treated independently
    all_ratios = []
    for query_id in selected_queries:
        qr = results["queries"][query_id]
        master_rep = get_representative(qr["master"]["times"])
        if not master_rep:
            continue

        # patch (prefetch=off)
        patch_off_rep = get_representative(qr["patch_off"]["times"])
        if patch_off_rep:
            ratio = patch_off_rep / master_rep
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=off",
                "ratio": ratio,
                "master_ms": master_rep,
                "patch_ms": patch_off_rep,
                "master_runs": len(qr["master"]["times"]),
                "patch_runs": len(qr["patch_off"]["times"]),
            })

        # patch (prefetch=on)
        patch_on_rep = get_representative(qr["patch_on"]["times"])
        if patch_on_rep:
            ratio = patch_on_rep / master_rep
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=on",
                "ratio": ratio,
                "master_ms": master_rep,
                "patch_ms": patch_on_rep,
                "master_runs": len(qr["master"]["times"]),
                "patch_runs": len(qr["patch_on"]["times"]),
            })

    print_ratio_rankings(all_ratios, args, baseline_name, baseline_label)

    # Return the per-query ratio entries so a cross-suite caller can pool them
    # into one combined ranking + geomean (see main()'s --queries path).
    return all_ratios


def run_stress_test(args):
    """Run stress test mode: randomly generate queries to find regressions."""
    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    baseline_name = getattr(args, "baseline", "master")
    baseline_label = baseline_name.upper()
    baseline_bin_orig, baseline_data_dir, baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    testbranch_name = getattr(args, "testbranch", "patch")
    test_bin_orig, test_data_dir, test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]
    master_bin = baseline_bin_orig
    patch_bin = test_bin_orig
    nvme_awake = nvme_kept_awake()
    note_nvme_power_state(args, nvme_awake)

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(baseline_bin_orig, tmpfs_mount, "baseline")
        patch_bin = copy_binaries_to_tmpfs(test_bin_orig, tmpfs_mount, "testbranch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  baseline ({baseline_name}): {master_bin}")
        print(f"  testbranch ({testbranch_name}): {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    # In cached mode, single-sample comparisons are too noisy for small
    # regressions.  Use multiple runs per query and compare representative
    # values (min or median) instead.
    stress_runs = args.runs if args.runs is not None else (RUNS_MIN if args.cached else 1)

    print("=" * 60)
    print("STRESS TEST MODE")
    print("=" * 60)
    print(f"Looking for regressions >= {STRESS_REGRESSION_THRESHOLD:.0%} slower than {baseline_name}")
    print(f"Generating {STRESS_QUERIES_PER_BATCH} queries per batch")
    print(f"Runs per query: {stress_runs}" + (" (cached mode)" if stress_runs > 1 else ""))
    print(f"Minimum query duration: {args.min_query_ms:.1f} ms")
    mode_word = ("\033[32m💵  cached\033[0m" if args.cached
                 else "\033[34m💾  uncached\033[0m")
    print(f"Mode: {mode_word}")
    print("=" * 60)

    # Get git hashes
    master_hash = get_git_hash(baseline_source_dir)
    patch_hash = get_git_hash(test_source_dir)
    print(f"\nBaseline ({baseline_name}) git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")
    print_io_settings(args)

    # We assume data is already loaded (--skip-load behavior for stress test)
    # User should run the normal benchmark first to ensure data exists

    iteration = 0
    total_queries_tested = 0

    try:
        while True:
            iteration += 1
            print(f"\n{'=' * 60}")
            print(f"ITERATION {iteration}")
            print(f"{'=' * 60}")

            # Generate batch of random queries
            queries = []
            for i in range(STRESS_QUERIES_PER_BATCH):
                query_num = total_queries_tested + i + 1
                queries.append((f"S{query_num}", generate_random_query(query_num)))

            # Print generated queries
            print(f"\nGenerated {len(queries)} queries:")
            for query_id, query_def in queries:
                print(f"  {query_id}: {query_def['name']}")

            # Results storage for this batch
            results = {}
            for query_id, query_def in queries:
                results[query_id] = {
                    "query_def": query_def,
                    "master": {"times": [], "min": None, "explain": None},
                    "patch_off": {"times": [], "min": None, "explain": None},
                    "patch_on": {"times": [], "min": None, "explain": None},
                }

            # Run all queries on baseline, replacing any that are too fast
            print(f"\n--- Running on BASELINE ({baseline_name}) ---")
            start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
            try:
                master_conn = psycopg.connect(**baseline_conn)
                pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                with master_conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                    # Stress test GUC settings
                    cur.execute("SET enable_bitmapscan = off")
                    cur.execute("SET random_page_cost = 1.1")
                    cur.execute("SET max_parallel_workers_per_gather = 0")

                # Process each query slot, replacing too-fast queries
                for i in range(len(queries)):
                    query_id, query_def = queries[i]
                    replacement_count = 0

                    while True:
                        print(f"\n  {query_id}: {query_def['name']}")
                        # Print query text
                        for line in query_def['sql'].strip().split('\n'):
                            print(f"    {line.strip()}")
                        print(f"  Running...", end=" ", flush=True)
                        try:
                            prepare_query(master_conn, query_def["sql"],
                                          gucs=query_def.get("gucs", {}), is_master=True,
                                          stmt_name=bench_stmt_name(query_id))
                            exec_time, explain_output = run_query(
                                master_conn, query_def, args.cached,
                                is_master=True, prefetch_setting=None,
                                benchmark_cpu=args.benchmark_cpu,
                                direct_io=server_direct_io(args, on_patch=False)
                            )
                            if exec_time is not None:
                                if exec_time < args.min_query_ms:
                                    deallocate_query(master_conn)
                                    replacement_count += 1
                                    print(f"{exec_time:.3f} ms (too fast, regenerating...)")
                                    # Generate a replacement query with same ID
                                    query_num = int(query_id[1:])  # Extract number from "S123"
                                    query_def = generate_random_query(query_num + replacement_count * 1000)
                                    queries[i] = (query_id, query_def)
                                    # Update results dict for new query
                                    results[query_id] = {
                                        "query_def": query_def,
                                        "master": {"times": [], "min": None, "explain": None},
                                        "patch_off": {"times": [], "min": None, "explain": None},
                                        "patch_on": {"times": [], "min": None, "explain": None},
                                    }
                                    continue  # Try again with new query
                                else:
                                    results[query_id]["master"]["times"].append(exec_time)
                                    results[query_id]["master"]["explain"] = explain_output
                                    print(f"{exec_time:.3f} ms")
                                    # Additional runs for cached mode
                                    for extra in range(stress_runs - 1):
                                        t, _ = run_query(
                                            master_conn, query_def, args.cached,
                                            is_master=True, prefetch_setting=None,
                                            benchmark_cpu=args.benchmark_cpu,
                                            direct_io=server_direct_io(args, on_patch=False),
                                            skip_prewarm=args.cached
                                        )
                                        if t is not None:
                                            results[query_id]["master"]["times"].append(t)
                                            print(f"  Run {extra + 2}: {t:.3f} ms")
                                    results[query_id]["master"]["min"] = get_representative(
                                        results[query_id]["master"]["times"])
                                    deallocate_query(master_conn)
                                    break  # Query is acceptable, move to next slot
                            else:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                        except Exception as e:
                            print(f"ERROR: {e}")
                            print("\nGenerated invalid SQL! This is a bug in the query generator.")
                            print(f"Query: {query_def['sql']}")
                            sys.exit(1)

                master_conn.close()
            finally:
                stop_server(master_bin, baseline_data_dir)
                time.sleep(2)

            # Run all queries on testbranch
            print(f"\n--- Running on TESTBRANCH ({testbranch_name}) ---")
            start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
            try:
                patch_conn = psycopg.connect(**test_conn)
                pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                with patch_conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                    # Stress test GUC settings
                    cur.execute("SET enable_bitmapscan = off")
                    cur.execute("SET random_page_cost = 1.1")
                    cur.execute("SET max_parallel_workers_per_gather = 0")

                # Tolerate a testbranch build that lacks the prefetch GUC (warns
                # + treats it as a baseline).  Once downgraded it stays False
                # across later batches and the re-confirm phases.
                test_has_prefetch = verify_test_has_prefetch(
                    patch_conn, test_has_prefetch, testbranch_name)

                BOLD = "\033[1m"
                RESET = "\033[0m"
                test_is_master = not test_has_prefetch

                for query_id, query_def in queries:
                    master_min = results[query_id]["master"]["min"]

                    print(f"\n  {query_id}: {query_def['name']}")
                    # Print query text
                    for line in query_def['sql'].strip().split('\n'):
                        print(f"    {line.strip()}")

                    prepare_query(patch_conn, query_def["sql"],
                                  gucs=query_def.get("gucs", {}), is_master=test_is_master,
                                  stmt_name=bench_stmt_name(query_id))

                    # Test with prefetch OFF (skip if --prefetch-only or no prefetch GUC)
                    if not args.prefetch_only and test_has_prefetch:
                        print(f"  prefetch=off...", end=" ", flush=True)
                        try:
                            for run_i in range(stress_runs):
                                exec_time, explain_output = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=False, prefetch_setting="off",
                                    benchmark_cpu=args.benchmark_cpu,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if exec_time is not None:
                                    results[query_id]["patch_off"]["times"].append(exec_time)
                                    if run_i == 0:
                                        results[query_id]["patch_off"]["explain"] = explain_output
                                    if stress_runs > 1:
                                        print(f"{exec_time:.3f}", end=" " if run_i < stress_runs - 1 else "", flush=True)
                                else:
                                    print("FAILED - could not extract execution time")
                                    sys.exit(1)
                            patch_off_rep = get_representative(results[query_id]["patch_off"]["times"])
                            results[query_id]["patch_off"]["min"] = patch_off_rep
                            if master_min:
                                ratio = patch_off_rep / master_min
                                if stress_runs > 1:
                                    print(f"→ {patch_off_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                                else:
                                    print(f"{patch_off_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                            else:
                                print(f"→ {patch_off_rep:.3f} ms" if stress_runs > 1 else f"{patch_off_rep:.3f} ms")
                        except Exception as e:
                            print(f"ERROR: {e}")
                            print("\nGenerated invalid SQL! This is a bug in the query generator.")
                            print(f"Query: {query_def['sql']}")
                            sys.exit(1)

                    # Test with prefetch ON (skip if --prefetch-disabled)
                    # When testbranch has no prefetch GUC, run queries plain
                    if not args.prefetch_disabled:
                        if test_has_prefetch:
                            print(f"  prefetch=on...", end=" ", flush=True)
                            pf_setting = "on"
                        else:
                            print(f"  running...", end=" ", flush=True)
                            pf_setting = None
                        try:
                            for run_i in range(stress_runs):
                                exec_time, explain_output = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=test_is_master, prefetch_setting=pf_setting,
                                    benchmark_cpu=args.benchmark_cpu,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if exec_time is not None:
                                    results[query_id]["patch_on"]["times"].append(exec_time)
                                    if run_i == 0:
                                        results[query_id]["patch_on"]["explain"] = explain_output
                                    if stress_runs > 1:
                                        print(f"{exec_time:.3f}", end=" " if run_i < stress_runs - 1 else "", flush=True)
                                else:
                                    print("FAILED - could not extract execution time")
                                    sys.exit(1)
                            patch_on_rep = get_representative(results[query_id]["patch_on"]["times"])
                            results[query_id]["patch_on"]["min"] = patch_on_rep
                            if master_min:
                                ratio = patch_on_rep / master_min
                                if stress_runs > 1:
                                    print(f"→ {patch_on_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                                else:
                                    print(f"{patch_on_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                            else:
                                print(f"→ {patch_on_rep:.3f} ms" if stress_runs > 1 else f"{patch_on_rep:.3f} ms")
                        except Exception as e:
                            print(f"ERROR: {e}")
                            print("\nGenerated invalid SQL! This is a bug in the query generator.")
                            print(f"Query: {query_def['sql']}")
                            sys.exit(1)

                    deallocate_query(patch_conn)

                patch_conn.close()
            finally:
                stop_server(patch_bin, test_data_dir)
                time.sleep(2)

            # Check for regressions
            print(f"\n--- Checking for regressions ---")
            regressions_found = []

            for query_id, query_def in queries:
                r = results[query_id]
                master_min = r["master"]["min"]
                patch_off_min = r["patch_off"]["min"]
                patch_on_min = r["patch_on"]["min"]

                if master_min is None:
                    continue

                # Check prefetch=off regression
                if patch_off_min is not None:
                    ratio_off = patch_off_min / master_min
                    if ratio_off >= STRESS_REGRESSION_THRESHOLD:
                        regressions_found.append({
                            "query_id": query_id,
                            "query_def": query_def,
                            "config": "prefetch=off",
                            "ratio": ratio_off,
                            "master_ms": master_min,
                            "patch_ms": patch_off_min,
                            "patch_on_ms": patch_on_min,
                        })

                # Check prefetch=on regression
                if patch_on_min is not None:
                    ratio_on = patch_on_min / master_min
                    if ratio_on >= STRESS_REGRESSION_THRESHOLD:
                        regressions_found.append({
                            "query_id": query_id,
                            "query_def": query_def,
                            "config": "prefetch=on",
                            "ratio": ratio_on,
                            "master_ms": master_min,
                            "patch_ms": patch_on_min,
                            "patch_off_ms": patch_off_min,
                        })

            total_queries_tested += len(queries)

            # Verify apparent regressions with retries to filter out spurious failures
            if regressions_found:
                print(f"\n--- Verifying {len(regressions_found)} apparent regression(s) with retries ---")
                confirmed_regressions = []
                needs_rebaseline = []  # Regressions that survived retries, need master re-baseline

                start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
                try:
                    patch_conn = psycopg.connect(**test_conn)
                    pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                    with patch_conn.cursor() as cur:
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                        cur.execute("SET enable_bitmapscan = off")
                        cur.execute("SET random_page_cost = 1.1")
                        cur.execute("SET max_parallel_workers_per_gather = 0")

                    for reg in regressions_found:
                        query_def = reg["query_def"]
                        master_avg = reg["master_ms"]
                        if test_has_prefetch:
                            prefetch_setting = "off" if reg["config"] == "prefetch=off" else "on"
                        else:
                            prefetch_setting = None

                        print(f"\n  Verifying {reg['query_id']} ({reg['config']}, initial {reg['ratio']:.3f}x)...")

                        prepare_query(patch_conn, query_def["sql"],
                                      gucs=query_def.get("gucs", {}),
                                      is_master=test_is_master, prefetch_setting=prefetch_setting,
                                      stmt_name=bench_stmt_name(reg["query_id"]))

                        # Simple retries: 3 attempts with short delay
                        RETRY_COUNT = 3
                        RETRY_DELAY = 1.0

                        regression_confirmed = True
                        for retry in range(RETRY_COUNT):
                            print(f"    Retry {retry + 1}/{RETRY_COUNT}...", end=" ", flush=True)
                            time.sleep(RETRY_DELAY)

                            retry_times = []
                            for run_i in range(stress_runs):
                                exec_time, _ = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=test_is_master, prefetch_setting=prefetch_setting,
                                    benchmark_cpu=args.benchmark_cpu,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if exec_time is not None:
                                    retry_times.append(exec_time)

                            if retry_times:
                                retry_rep = get_representative(retry_times)
                                ratio = retry_rep / master_avg
                                if stress_runs > 1:
                                    print(f"{' '.join(f'{t:.3f}' for t in retry_times)} → {retry_rep:.3f} ms ({ratio:.3f}x)")
                                else:
                                    print(f"{retry_rep:.3f} ms ({ratio:.3f}x)")

                                if ratio < STRESS_REGRESSION_THRESHOLD:
                                    print(f"    Under threshold - spurious failure, discarding")
                                    regression_confirmed = False
                                    break
                            else:
                                print("FAILED")

                        deallocate_query(patch_conn)

                        if regression_confirmed:
                            print(f"    Retries confirm regression - needs {baseline_name} re-baseline")
                            needs_rebaseline.append(reg)

                    patch_conn.close()
                finally:
                    stop_server(patch_bin, test_data_dir)
                    time.sleep(2)

                # Phase 2: Re-baseline verification for regressions that survived retries
                # This accounts for environmental drift since the original master baseline
                if needs_rebaseline:
                    print(f"\n--- Re-baselining {len(needs_rebaseline)} regression(s) against {baseline_name} ---")

                    for reg in needs_rebaseline:
                        query_def = reg["query_def"]
                        prefetch_setting = "off" if reg["config"] == "prefetch=off" else "on"

                        print(f"\n  Re-baselining {reg['query_id']} ({reg['config']})...")

                        # Run query on baseline to establish new baseline
                        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
                        try:
                            master_conn = psycopg.connect(**baseline_conn)
                            pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                            with master_conn.cursor() as cur:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                                cur.execute("SET enable_bitmapscan = off")
                                cur.execute("SET random_page_cost = 1.1")
                                cur.execute("SET max_parallel_workers_per_gather = 0")

                            prepare_query(master_conn, query_def["sql"],
                                          gucs=query_def.get("gucs", {}), is_master=True,
                                          stmt_name=bench_stmt_name(reg["query_id"]))
                            rebaseline_master_times = []
                            for run_i in range(stress_runs):
                                t, _ = run_query(
                                    master_conn, query_def, args.cached,
                                    is_master=True, prefetch_setting=None,
                                    benchmark_cpu=args.benchmark_cpu,
                                    direct_io=server_direct_io(args, on_patch=False),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if t is not None:
                                    rebaseline_master_times.append(t)
                            deallocate_query(master_conn)
                            master_conn.close()
                        finally:
                            stop_server(master_bin, baseline_data_dir)
                            time.sleep(2)

                        if not rebaseline_master_times:
                            print(f"    Baseline re-baseline FAILED, discarding regression")
                            continue

                        new_master_time = get_representative(rebaseline_master_times)
                        best_master_time = min(new_master_time, reg["master_ms"])
                        if stress_runs > 1:
                            print(f"    New {baseline_name} baseline: {' '.join(f'{t:.3f}' for t in rebaseline_master_times)} → {new_master_time:.3f} ms (was {reg['master_ms']:.3f} ms, using {best_master_time:.3f} ms)")
                        else:
                            print(f"    New {baseline_name} baseline: {new_master_time:.3f} ms (was {reg['master_ms']:.3f} ms, using {best_master_time:.3f} ms)")

                        # Run query on testbranch with new baseline
                        start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
                        try:
                            patch_conn = psycopg.connect(**test_conn)
                            pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                            with patch_conn.cursor() as cur:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                                cur.execute("SET enable_bitmapscan = off")
                                cur.execute("SET random_page_cost = 1.1")
                                cur.execute("SET max_parallel_workers_per_gather = 0")

                            prepare_query(patch_conn, query_def["sql"],
                                          gucs=query_def.get("gucs", {}),
                                          is_master=test_is_master, prefetch_setting=prefetch_setting,
                                          stmt_name=bench_stmt_name(reg["query_id"]))
                            rebaseline_patch_times = []
                            for run_i in range(stress_runs):
                                t, _ = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=test_is_master, prefetch_setting=prefetch_setting,
                                    benchmark_cpu=args.benchmark_cpu,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if t is not None:
                                    rebaseline_patch_times.append(t)
                            deallocate_query(patch_conn)
                            patch_conn.close()
                        finally:
                            stop_server(patch_bin, test_data_dir)
                            time.sleep(2)

                        if not rebaseline_patch_times:
                            print(f"    Patch re-run FAILED, discarding regression")
                            continue

                        new_patch_time = get_representative(rebaseline_patch_times)
                        best_patch_time = min(new_patch_time, reg["patch_ms"])
                        best_ratio = best_patch_time / best_master_time
                        if stress_runs > 1:
                            print(f"    New patch time: {' '.join(f'{t:.3f}' for t in rebaseline_patch_times)} → {new_patch_time:.3f} ms (was {reg['patch_ms']:.3f} ms, using {best_patch_time:.3f} ms)")
                        else:
                            print(f"    New patch time: {new_patch_time:.3f} ms (was {reg['patch_ms']:.3f} ms, using {best_patch_time:.3f} ms)")
                        print(f"    Best ratio: {best_ratio:.3f}x (orig {reg['ratio']:.3f}x, rebaseline {new_patch_time / new_master_time:.3f}x)")

                        if best_ratio >= STRESS_REGRESSION_THRESHOLD:
                            print(f"    Regression CONFIRMED with fresh baseline")
                            # Update reg with best measurements from either run
                            reg["master_ms"] = best_master_time
                            reg["patch_ms"] = best_patch_time
                            reg["ratio"] = best_ratio
                            confirmed_regressions.append(reg)
                        else:
                            print(f"    Below threshold - discarding (orig {reg['ratio']:.3f}x, rebaseline {new_patch_time / new_master_time:.3f}x, best {best_ratio:.3f}x)")

                regressions_found = confirmed_regressions

            if regressions_found:
                # Sort by ratio (worst first)
                regressions_found.sort(key=lambda x: x["ratio"], reverse=True)
                worst = regressions_found[0]

                failure_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print()
                print("=" * 60)
                print("REGRESSION FOUND!")
                print(f"Time: {failure_time}")
                print("=" * 60)
                pct = (worst["ratio"] - 1) * 100
                print(f"Patch ({worst['config']}) is {worst['ratio']:.3f}x slower than {baseline_name} ({pct:.1f}% regression)")
                print()
                print(f"{baseline_label + ':':18s}{worst['master_ms']:.3f} ms")

                query_def = worst["query_def"]
                r = results[worst["query_id"]]

                if r["patch_off"]["min"]:
                    ratio_off = r["patch_off"]["min"] / worst["master_ms"]
                    marker = " <-- REGRESSION" if worst["config"] == "prefetch=off" else ""
                    print(f"Patch (off):      {r['patch_off']['min']:.3f} ms ({ratio_off:.3f}x vs {baseline_name}){marker}")

                if r["patch_on"]["min"]:
                    ratio_on = r["patch_on"]["min"] / worst["master_ms"]
                    marker = " <-- REGRESSION" if worst["config"] == "prefetch=on" else ""
                    print(f"Patch (on):       {r['patch_on']['min']:.3f} ms ({ratio_on:.3f}x vs {baseline_name}){marker}")

                # Print EXPLAIN ANALYZE output
                print()
                print("EXPLAIN ANALYZE output:")
                print()
                if r["master"]["explain"]:
                    print(f"  {baseline_name}:")
                    for line in r["master"]["explain"].split('\n'):
                        print(f"    {line}")
                if r["patch_off"]["explain"]:
                    print()
                    print("  patch (prefetch=off):")
                    for line in r["patch_off"]["explain"].split('\n'):
                        print(f"    {line}")
                if r["patch_on"]["explain"]:
                    print()
                    print("  patch (prefetch=on):")
                    for line in r["patch_on"]["explain"].split('\n'):
                        print(f"    {line}")

                # Print query definition ready to paste into a suite .toml file
                print()
                print("Add this to a suite's .toml file (e.g. suites/00-benchmark.toml):")
                print()

                # Format evict / prewarm lists as TOML arrays of strings
                evict_str = ", ".join(f'"{t}"' for t in query_def["evict"])
                prewarm_idx_str = ", ".join(f'"{i}"' for i in query_def["prewarm_indexes"])
                prewarm_tbl_str = ", ".join(f'"{t}"' for t in query_def["prewarm_tables"])

                # Clean up SQL formatting
                sql_lines = query_def["sql"].strip().split('\n')
                sql_formatted = '\n'.join('            ' + line.strip() for line in sql_lines)

                print(f'[queries.STRESS_{total_queries_tested}]')
                print(f'name = "{query_def["name"]}"')
                print("sql = '''")
                print(sql_formatted)
                print("'''")
                print(f'evict = [{evict_str}]')
                print(f'prewarm_indexes = [{prewarm_idx_str}]')
                print(f'prewarm_tables = [{prewarm_tbl_str}]')
                if query_def.get("gucs"):
                    gucs_str = ", ".join(f'"{k}" = "{v}"' for k, v in query_def["gucs"].items())
                    print(f'gucs = {{ {gucs_str} }}')
                print()
                print(f"Total queries tested: {total_queries_tested}")
                print(f"Iterations: {iteration}")
                return  # Stop on confirmed regression

            else:
                print(f"No regressions found in this batch.")
                print(f"Total queries tested so far: {total_queries_tested}")
                print("Generating next batch...")

    except KeyboardInterrupt:
        print(f"\n\nStress test interrupted after {total_queries_tested} queries ({iteration} iterations)")
        print("No regressions found.")
    finally:
        # Cleanup tmpfs if it was created
        if tmpfs_mount:
            cleanup_tmpfs(tmpfs_mount)


def _format_duration(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins == 0:
        return f"{secs} seconds"
    elif mins == 1:
        return f"1 minute {secs} seconds"
    else:
        return f"{mins} minutes {secs} seconds"


def _print_wall_time(wall_start):
    wall_end = time.time()
    start_dt = datetime.fromtimestamp(wall_start)
    end_dt = datetime.fromtimestamp(wall_end)
    print(f"\n{'=' * 60}")
    print(f"Started:       {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Finished:      {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total runtime: {_format_duration(wall_end - wall_start)}")
    print(f"{'=' * 60}")

    # Re-surface the missing-prefetch-GUC warning as the very last output so it
    # isn't buried in the scrollback of a long run.
    if MISSING_PREFETCH_GUC_TESTBRANCH is not None:
        print_missing_prefetch_guc_warning(MISSING_PREFETCH_GUC_TESTBRANCH)
    if NVME_ASLEEP_WARNING is not None:
        print_nvme_asleep_warning(NVME_ASLEEP_WARNING)


def main():
    global USE_MEDIAN
    wall_start = time.time()
    args = parse_arguments()
    args = resolve_io_method_defaults(args)

    # Set global median flag
    USE_MEDIAN = args.use_median

    # Update data dirs if using delay pgdata
    if args.delay_pgdata:
        # Update baseline config
        base_bin, _base_data, base_src, base_conn = BASELINE_CONFIGS[args.baseline]
        base_dir = os.path.dirname(_base_data)
        base_delay = os.path.join(base_dir, "data-delay")
        BASELINE_CONFIGS[args.baseline] = (base_bin, base_delay, base_src, base_conn)
        # Update testbranch config
        test_bin, _test_data, test_src, test_conn = TESTBRANCH_CONFIGS[args.testbranch]
        test_dir = os.path.dirname(_test_data)
        test_delay = os.path.join(test_dir, "data-delay")
        TESTBRANCH_CONFIGS[args.testbranch] = (test_bin, test_delay, test_src, test_conn)
        print(f"Using delayed I/O data directories:")
        print(f"  Baseline ({args.baseline}): {base_delay}")
        print(f"  Testbranch ({args.testbranch}): {test_delay}")

    if args.list_modes:
        modes_json = []
        for suite in BENCHMARK_SUITES:
            modes_json.append({
                "mode_prefix": suite["mode_prefix"],
                "cli_flag": suite["cli_flag"],
                "uncached_runs": suite["uncached_runs"],
                "cached_runs": suite["cached_runs"],
            })
        print(json.dumps(modes_json))
        return

    # Check every server binary against its data directory before anything
    # else touches the machine (interferer kill, tmpfs, data load): a stale
    # install -- master not rebuilt across a catversion bump, say -- fails
    # here, at once.
    preflight_servers(args)

    # Stop background processes (vscode-server, claude) that would skew
    # results.  Interactive: prompts (answer n to keep them running while
    # debugging); non-interactive: --force.  Done after metadata-only modes
    # like --list-modes so they don't disturb the machine.
    if args.no_kill_interferers:
        print("Skipping kill_vscode_server.sh (--no-kill-interferers); "
              "interfering processes may add measurement noise.")
    else:
        kill_interfering_processes()

    # Verify THP / CPU governor are benchmark-friendly (warns + prompts, aborts
    # if declined).  Same placement rationale as the kill above.
    check_benchmark_env()

    if args.stress_test:
        run_stress_test(args)
        _print_wall_time(wall_start)
        return

    # Enforce globally-unique query ids and build the id -> suite lookup.
    suite_map = build_query_suite_map()

    cache_tag = "cached" if args.cached else "uncached"

    if args.queries:
        # --queries is authoritative and may span several suites: resolve each
        # requested id to its owning suite, then run each involved suite in turn
        # with only its subset.  This decouples query selection from the suite
        # flags, so `--queries WR1,Q1` works without `--worker-regress-feb`.
        requested = [q.strip().upper() for q in args.queries.split(",") if q.strip()]
        unknown = [q for q in requested if q not in suite_map]
        if unknown:
            all_ids = [qid for s in BENCHMARK_SUITES for qid in s["queries"].keys()]
            print(f"Error: Unknown query {', '.join(unknown)}. "
                  f"Available: {', '.join(all_ids)}")
            sys.exit(1)

        # Group requested ids by owning suite, preserving each suite's first
        # appearance in the --queries list; within a suite, keep listed order.
        grouped = OrderedDict()  # mode_prefix -> (suite, [qids])
        for qid in requested:
            suite = suite_map[qid]
            key = suite["mode_prefix"]
            if key not in grouped:
                grouped[key] = (suite, [])
            grouped[key][1].append(qid)

        multi = len(grouped) > 1
        combined_ratios = []
        for suite, qids in grouped.values():
            if multi:
                print(f"\n{'#' * 60}")
                print(f"# Suite: {suite['mode_prefix']}  ({', '.join(qids)})")
                print(f"{'#' * 60}")
            suite_ratios = run_generic_benchmark(
                args, suite["queries"],
                f"{suite['mode_prefix']}_{cache_tag}",
                suite["title"],
                suite["verify_fn"], suite["load_fn"],
                sync_stats=suite.get("sync_stats", False),
                tables=suite.get("tables"),
                expect_all_visible=suite.get("expect_all_visible", True),
                query_ids=qids)
            if suite_ratios:
                combined_ratios.extend(suite_ratios)

        # When the run spanned several suites, pool every query into one
        # ranking + geomean so the cross-suite result reads as a single set.
        # Query ids are globally unique, so there are no collisions to resolve.
        if multi and combined_ratios:
            baseline_name = getattr(args, "baseline", "master")
            baseline_label = baseline_name.upper()
            print(f"\n{'#' * 60}")
            print(f"# COMBINED RESULTS ACROSS ALL SUITES "
                  f"({len({e['query_id'] for e in combined_ratios})} queries)")
            print(f"{'#' * 60}")
            print_ratio_rankings(combined_ratios, args, baseline_name,
                                 baseline_label, scope="ALL SUITES — ")

        _print_wall_time(wall_start)
        return

    # No --queries: select a single suite by its flag (default is "benchmark").
    selected = BENCHMARK_SUITES[0]
    for suite in BENCHMARK_SUITES[1:]:
        if suite["cli_dest"] and getattr(args, suite["cli_dest"], False):
            selected = suite
            break

    run_generic_benchmark(args, selected["queries"],
                          f"{selected['mode_prefix']}_{cache_tag}",
                          selected["title"],
                          selected["verify_fn"], selected["load_fn"],
                          sync_stats=selected.get("sync_stats", False),
                          tables=selected.get("tables"),
                          expect_all_visible=selected.get("expect_all_visible", True))
    _print_wall_time(wall_start)


if __name__ == "__main__":
    main()
