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
"""

import argparse
import os
import random
import shutil
import signal
import subprocess
import sys
import time

import psycopg

# os.environ["MALLOPT_TOP_PAD_"] = str(64 * 1024 * 1024)
# os.environ["MALLOPT_TOP_PAD"] = str(64 * 1024 * 1024)
# os.environ["M_TOP_PAD"] = str(64 * 1024 * 1024)
# os.environ["M_MMAP_THRESHOLD"] = str(64 * 1024 * 1024)
# os.environ["M_TRIM_THRESHOLD"] = str(64 * 1024 * 1024)
# os.environ["M_MMAP_MAX"] = str(0)
# os.environ["M_ARENA_TEST"] = str(64)

# --- Configuration ---

# Paths to the PostgreSQL binaries for the two versions you want to compare.
MASTER_BIN="/mnt/nvme/postgresql/master/install_meson_rc/bin"
PATCH_BIN="/mnt/nvme/postgresql/patch/install_meson_rc/bin"

# Data directories for the two PostgreSQL instances.
MASTER_DATA_DIR="/mnt/nvme/postgresql/master/data"
PATCH_DATA_DIR="/mnt/nvme/postgresql/patch/data"

# Connection details for the two PostgreSQL instances.
# Using a dictionary for easier access.
MASTER_CONN_DETAILS = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5555
}
PATCH_CONN_DETAILS = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp", # Use socket for local connections
    "port": 5432
}

# The frequency of 'perf' sampling.
PERF_FREQUENCY=9999

# --- Script variables ---
FLAMEGRAPH_DIR="/home/pg/code/FlameGraph"
OUTPUT_DIR = "output_perf_flamegraph"

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
        action="store_true",
        help="Run perf profiling (disabled by default)"
    )
    parser.add_argument(
        "--benchmark",
        choices=["nestloop", "simple_select"],
        default="nestloop",
        help="Benchmark to run (default: nestloop)"
    )
    parser.add_argument(
        "--perf-event", "--event",
        type=str,
        dest="perf_event",
        help="Perf event to profile (only valid with --perf; default: perf default)"
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
        "--queries",
        type=int,
        dest="num_queries",
        help="Number of queries to execute (only valid with --benchmark simple_select)"
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
    return parser.parse_args()

def check_dependencies():
    """Check if required tools are available."""
    for cmd in ["perf", "git"]:
        if not shutil.which(cmd):
            print(f"Error: Command '{cmd}' not found. Please install it.")
            sys.exit(1)
    if not os.path.isdir(FLAMEGRAPH_DIR):
        raise FileNotFoundError("no FlameGraph repository")

def start_server(pg_bin_dir, pg_name, pg_data_dir, conn_details):
    """Start a PostgreSQL server and wait for it to be ready."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    log_file = os.path.join(OUTPUT_DIR, f"{pg_name}.postgres_log")

    # Ensure server is stopped before we start
    if subprocess.run([pg_ctl_path, "status", "-D", pg_data_dir], check=False).returncode == 0:
        print(f"{pg_name}: Server is already running. Stopping it...")
        subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
        time.sleep(3)

    # Start the server
    print(f"Starting {pg_name} PostgreSQL server (port {conn_details.get('port', 'default')})...")
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    result = subprocess.run(
        [pg_ctl_path, "start",
         "-o", "--autovacuum=off",
         "-D", pg_data_dir,
         "-l", log_file, "-o",
         start_options],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"Error: Failed to start {pg_name} server")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        print(f"Check log file: {log_file}")
        # Try to read the actual server log if it exists
        try:
            with open(log_file, 'r') as f:
                print(f"Log file contents:\n{f.read()}")
        except FileNotFoundError:
            pass
        sys.exit(1)

    # Wait for the server to be ready
    print(f"Waiting for {pg_name} server to accept connections...")
    for attempt in range(15):
        try:
            conn = psycopg.connect(**conn_details, connect_timeout=2)
            conn.close()
            print(f"{pg_name} server started successfully.")
            return
        except (psycopg.OperationalError, psycopg.DatabaseError) as e:
            if attempt == 14:
                print(f"Error: {pg_name} server failed to start or become available after 15 attempts")
                print(f"Last connection error: {e}")
                sys.exit(1)
            if attempt < 5:
                time.sleep(0.5)
            else:
                time.sleep(1)

def stop_server(pg_bin_dir, pg_data_dir):
    """Stop a PostgreSQL server (gracefully handles if not running)."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    print("Stopping PostgreSQL server...")
    # Don't fail if server is not running
    subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=False)

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

def profile_postgres(pg_bin_dir, pg_name, conn_details, output_file, sql_query, query_repetitions, run_perf, max_aid_val=None, perf_event=None, highfreq=False, index_only_scan=False):
    """Profiles a PostgreSQL instance (assumes server is already running)."""
    print(f"--- Testing {pg_name} ---")

    conn = None
    try:
        # Connect to the database to get the backend PID
        conn = psycopg.connect(**conn_details, prepare_threshold=0)

        backend_pid = conn.info.backend_pid
        print(f"Successfully connected. Backend PID is: {backend_pid}")

        # Pin the backend process to a specific core.
        # This may require running the script with sufficient privileges (e.g., as root).
        try:
            # Pin backend to core 1
            os.sched_setaffinity(backend_pid, {1})
            print(f"Pinned backend PID {backend_pid} to CPU 1.")
        except (AttributeError, PermissionError, OSError) as e:
            print(f"Warning: Could not set CPU affinity for backend PID {backend_pid}: {e}")

        print("Prewarming...")
        with conn.cursor() as cursor:
            # if pg_name == "patch":
            #     cursor.execute("set enable_indexscan_prefetch=off;")
            cursor.execute("set enable_bitmapscan=off;")
            cursor.execute("set enable_hashjoin=off;")
            if index_only_scan:
                cursor.execute("set enable_indexonlyscan=on;")
            else:
                cursor.execute("set enable_indexonlyscan=off;")
            cursor.execute("set enable_material=off;")
            cursor.execute("set enable_memoize=off;")
            cursor.execute("set enable_mergejoin=off;")
            cursor.execute("set enable_seqscan=off;")

            cursor.execute("set max_parallel_workers_per_gather = 0;")
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

        perf_process = None
        perf_command = []
        if run_perf:
            print(f"Starting perf on PID {backend_pid}...")
            perf_freq = "49999" if highfreq else str(PERF_FREQUENCY)
            perf_command = [
                "perf", "record",
                "-F", perf_freq,
            ]
            # Only add -e flag if a specific perf event was requested
            if perf_event:
                perf_command.extend(["-e", perf_event])

            perf_command.extend([
                # "-e", "alignment-faults",
                # "-e", "branch-instructions",
                # "-e", "branch-misses",
                # "-e", "branches",
                # "-e", "cache-misses",
                # "-e", "cache-references",
                # "-e", "cgroup-switches",
                # "-e", "context-switches",
                # "-e", "cpu-clock",
                # "-e", "cpu-cycles",
                # "-e", "cpu-migrations",
                # "-e", "instructions",
                # "-e", "major-faults",
                # "-e", "minor-faults",
                # "-e", "page-faults",
                # "-e", "stalled-cycles-frontend",
                # "-e", "task-clock",

                #### AMD recommended events usable from thread level ###

                # [All L1 Data Cache Accesses. Unit: cpu]
                # "-e", "all_data_cache_accesses",
                # [All TLBs Flushed. Unit: cpu]
                # "-e", "all_tlbs_flushed",
                # [L1 Data Cache Fills: All. Unit: cpu]
                # "-e", "l1_data_cache_fills_all",
                # [L1 Data Cache Fills: From External CCX Cache. Unit: cpu]
                # "-e", "l1_data_cache_fills_from_external_ccx_cache",
                # [L1 Data Cache Fills: From Memory. Unit: cpu]
                # "-e", "l1_data_cache_fills_from_memory",
                # [L1 Data Cache Fills: From Remote Node.  Unit: cpu]
                # "-e", "l1_data_cache_fills_from_remote_node",
                # [L1 Data Cache Fills: From within same CCX.  Unit: cpu]
                # "-e", "l1_data_cache_fills_from_within_same_ccx",
                # [L1 DTLB Misses.  Unit: cpu]
                # "-e", "l1_dtlb_misses",
                # [L2 Cache Accesses from L1 Data Cache Misses (including prefetch).  Unit: cpu]
                # "-e", "l2_cache_accesses_from_dc_misses",
                # [L2 Cache Accesses from L1 Instruction Cache Misses (including prefetch).  Unit: cpu]
                # "-e", "l2_cache_accesses_from_ic_misses",
                # [L2 Cache Hits from L1 Data Cache Misses.  Unit: cpu]
                # "-e", "l2_cache_hits_from_dc_misses",
                # [L2 Cache Hits from L1 Instruction Cache Misses.  Unit: cpu]
                # "-e", "l2_cache_hits_from_ic_misses",
                # [L2 Cache Hits from L2 Cache HWPF.  Unit: cpu]
                # "-e", "l2_cache_hits_from_l2_hwpf",
                # [L2 Cache Misses from L1 Data Cache Misses.  Unit: cpu]
                # "-e", "l2_cache_misses_from_dc_misses",
                # [L2 Cache Misses from L1 Instruction Cache Misses.  Unit: cpu]
                # "-e", "l2_cache_misses_from_ic_miss",
                # [L2 DTLB Misses & Data page walks.  Unit: cpu]
                # "-e", "l2_dtlb_misses",
                # [L2 ITLB Misses & Instruction page walks.  Unit: cpu]
                # "-e", "l2_itlb_misses",
                # [Macro-ops Retired.  Unit: cpu]
                # "-e", "macro_ops_retired",
                # [Mixed SSE/AVX Stalls.  Unit: cpu]
                # "-e", "sse_avx_stalls",

                #### AMD recommended events unusable from thread level ###

                # [L3 Cache Accesses.  Unit: amd_l3]
                # "-e", "l3_cache_accesses",
                # [L3 Misses (includes cacheline state change requests).  Unit: amd_l3]
                # "-e", "l3_misses",

                "-p", str(backend_pid),
                # "-a",
                "-g",
                # "--call-graph", "dwarf",
                "-o",  OUTPUT_DIR + "/" + pg_name,
            ])
            perf_process = subprocess.Popen(perf_command)

            # Pin perf process to a different core to minimize interference.
            try:
                # Pin perf to core 2
                os.sched_setaffinity(perf_process.pid, {2})
                print(f"Pinned perf PID {perf_process.pid} to CPU 2.")
            except (AttributeError, PermissionError, OSError) as e:
                print(f"Warning: Could not set CPU affinity for perf PID {perf_process.pid}: {e}")

            # Give perf a moment to initialize before starting the workload
            time.sleep(1)

        # Show EXPLAIN plan before starting the benchmark
        print("\n--- Query Plan (EXPLAIN) ---")
        with conn.cursor() as cursor: # type: ignore
            explain_query = f"EXPLAIN {sql_query}"
            if max_aid_val is not None:
                cursor.execute(explain_query, params=[random.randint(1, max_aid_val)], prepare=False)
            else:
                cursor.execute(explain_query, prepare=False)
            for row in cursor.fetchall():
                print(row[0])
        print("--- End Query Plan ---\n")

        # Execute the query repeatedly in the same connection
        print(f"Executing the SQL query {query_repetitions} times...")
        random.seed(42)
        start_time = time.time()
        with conn.cursor() as cursor: # type: ignore
            for _ in range(query_repetitions):
                if max_aid_val is not None:
                    cursor.execute(query=sql_query,
                                   params=[random.randint(1, max_aid_val)],
                                   prepare=True)
                else:
                    cursor.execute(query=sql_query, prepare=True)
        end_time = time.time()
        total_time = end_time - start_time
        print(f"Query loop finished in \033[1m{total_time:.3f} seconds\033[0m")

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
                sed_process = subprocess.run(
                    ["sed", sed_expression],
                    stdin=perf_script_process.stdout,
                    stdout=f,
                    check=True
                )
            perf_script_process.stdout.close()
            perf_script_process.wait()

    finally:
        # Ensure the connection is closed
        if conn:
            conn.close()
        print("----------------------------------------")

    # Return string of perf command for flamegraph --subtitle arg
    return ' '.join(perf_command), total_time

def main():
    """Main execution flow."""
    args = parse_arguments()

    # Validate that --perf-event is only used with --perf
    if args.perf_event and not args.perf:
        print("Error: --perf-event can only be used with --perf")
        sys.exit(1)

    # Validate that --queries is only used with --benchmark simple_select
    if args.num_queries is not None and args.benchmark != "simple_select":
        print("Error: --queries can only be used with --benchmark simple_select")
        sys.exit(1)

    # Validate that --hash and --ios are not used together
    if args.use_hash_index and args.index_only_scan:
        print("Error: --hash and --ios cannot be used together (hash indexes lack support for index-only scans)")
        sys.exit(1)

    # Pin the script itself to a core to avoid it interfering with the benchmark.
    # This may require running the script with sufficient privileges (e.g., as root).
    try:
        # Pin this script to core 0
        pid = os.getpid()
        os.sched_setaffinity(pid, {0})
        print(f"Pinned this script (PID {pid}) to CPU 0.")
    except (AttributeError, PermissionError, OSError) as e:
        print(f"Warning: Could not set CPU affinity for this script: {e}")

    check_dependencies()

    # Get benchmark configuration
    benchmark = BENCHMARKS[args.benchmark]
    sql_query = benchmark["sql_query"]
    query_repetitions = benchmark["query_repetitions"]
    max_aid_val = benchmark.get("max_aid_val")

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
            start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
            init_pgbench(MASTER_BIN, MASTER_CONN_DETAILS, args.pgbench_scale, args.use_hash_index)
            stop_server(MASTER_BIN, MASTER_DATA_DIR)
            time.sleep(2)

            print("--- Initializing pgbench for patch ---")
            start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
            init_pgbench(PATCH_BIN, PATCH_CONN_DETAILS, args.pgbench_scale, args.use_hash_index)
            stop_server(PATCH_BIN, PATCH_DATA_DIR)
            time.sleep(2)
        else:
            print("Skipping pgbench initialization.")
            # Verify that existing indexes match the requested type
            if args.use_hash_index:
                print("\n--- Verifying existing hash indexes ---")
                start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
                verify_hash_index_exists("master", MASTER_CONN_DETAILS)
                stop_server(MASTER_BIN, MASTER_DATA_DIR)
                time.sleep(1)

                start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
                verify_hash_index_exists("patch", PATCH_CONN_DETAILS)
                stop_server(PATCH_BIN, PATCH_DATA_DIR)
                time.sleep(1)
            else:
                print("\n--- Verifying existing B-tree indexes ---")
                start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
                verify_btree_index_exists("master", MASTER_CONN_DETAILS)
                stop_server(MASTER_BIN, MASTER_DATA_DIR)
                time.sleep(1)

                start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
                verify_btree_index_exists("patch", PATCH_CONN_DETAILS)
                stop_server(PATCH_BIN, PATCH_DATA_DIR)
                time.sleep(1)

        # Get row counts from both servers and verify they match
        print("\n--- Verifying pgbench_accounts row counts ---")
        start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
        master_row_count = get_pgbench_row_count("master", MASTER_CONN_DETAILS)
        stop_server(MASTER_BIN, MASTER_DATA_DIR)
        time.sleep(1)

        start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
        patch_row_count = get_pgbench_row_count("patch", PATCH_CONN_DETAILS)
        stop_server(PATCH_BIN, PATCH_DATA_DIR)
        time.sleep(1)

        if master_row_count != patch_row_count:
            print(f"Error: Row count mismatch! master={master_row_count}, patch={patch_row_count}")
            sys.exit(1)

        # Use the actual row count for simple_select benchmark
        if args.benchmark == "simple_select":
            max_aid_val = master_row_count
            print(f"Using max_aid_val={max_aid_val} for simple_select benchmark")

        # Profile in the order specified by args.patch_first
        if args.patch_first:
            # Profile patch first
            print("--- Profiling patch version ---")
            start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
            perf_command_patch, total_time_patch = profile_postgres(
                PATCH_BIN, "patch", PATCH_CONN_DETAILS,
                stacks_file_patch, sql_query, query_repetitions, args.perf, max_aid_val, args.perf_event, args.highfreq, args.index_only_scan)
            stop_server(PATCH_BIN, PATCH_DATA_DIR)
            time.sleep(2)

            # Then profile master
            print("--- Profiling master version ---")
            start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
            perf_command_master, total_time_master = profile_postgres(
                MASTER_BIN, "master", MASTER_CONN_DETAILS,
                stacks_file_master, sql_query, query_repetitions, args.perf, max_aid_val, args.perf_event, args.highfreq, args.index_only_scan)
            stop_server(MASTER_BIN, MASTER_DATA_DIR)
        else:
            # Profile master first (default)
            print("--- Profiling master version ---")
            start_server(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS)
            perf_command_master, total_time_master = profile_postgres(
                MASTER_BIN, "master", MASTER_CONN_DETAILS,
                stacks_file_master, sql_query, query_repetitions, args.perf, max_aid_val, args.perf_event, args.highfreq, args.index_only_scan)
            stop_server(MASTER_BIN, MASTER_DATA_DIR)
            time.sleep(2)

            # Then profile patch
            print("--- Profiling patch version ---")
            start_server(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS)
            perf_command_patch, total_time_patch = profile_postgres(
                PATCH_BIN, "patch", PATCH_CONN_DETAILS,
                stacks_file_patch, sql_query, query_repetitions, args.perf, max_aid_val, args.perf_event, args.highfreq, args.index_only_scan)
            stop_server(PATCH_BIN, PATCH_DATA_DIR)

    finally:
        # Always stop both servers
        print("--- Stopping PostgreSQL servers ---")
        stop_server(MASTER_BIN, MASTER_DATA_DIR)
        stop_server(PATCH_BIN, PATCH_DATA_DIR)

    print(f"Patch query loop took \033[1m{total_time_patch/total_time_master:.3f}x\033[0m as long as master")

    if not args.perf:
        print("Perf profiling was disabled. Exiting.")
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

    # Print perf recipe for comparing a specific function
    print("\n--- Perf Recipe for Function Comparison (master vs patch) ---")
    print("To compare a specific function between master and patch profiles, use:")
    print()
    print(f"  perf diff {OUTPUT_DIR}/master {OUTPUT_DIR}/patch")
    print()
    print("Replace 'master' and 'patch' with the perf.data file paths if using different locations.")
    print("This will show the differences in:")
    print("  - Sampling count")
    print("  - Instructions")
    print("  - Cache misses")
    print("  - Other performance metrics")
    print()
    print("To view a specific function in detail from master:")
    print(f"  perf report -i {OUTPUT_DIR}/master")
    print()
    print("To view a specific function in detail from patch:")
    print(f"  perf report -i {OUTPUT_DIR}/patch")
    print()

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
