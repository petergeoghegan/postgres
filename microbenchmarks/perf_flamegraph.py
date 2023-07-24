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

import os
import random
import shutil
import signal
import subprocess
import sys
import time

import psycopg

os.environ["MALLOPT_TOP_PAD_"] = str(64 * 1024 * 1024)
os.environ["MALLOPT_TOP_PAD"] = str(64 * 1024 * 1024)
os.environ["M_TOP_PAD"] = str(64 * 1024 * 1024)
os.environ["M_MMAP_THRESHOLD"] = str(64 * 1024 * 1024)
os.environ["M_TRIM_THRESHOLD"] = str(64 * 1024 * 1024)
os.environ["M_MMAP_MAX"] = str(0)
os.environ["M_ARENA_TEST"] = str(64)

# --- Configuration ---

SIMPLE_SELECT_PROFILING=False
RUN_PERF = False

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

if SIMPLE_SELECT_PROFILING:
    SQL_QUERY="select * from pgbench_accounts where aid = %s"
    QUERY_REPETITIONS=500_000
    MAX_AID_VAL=50_00_000
else:
    SQL_QUERY="select count(*) from pgbench_accounts a join pgbench_branches b on a.bid = b.bid"
    QUERY_REPETITIONS=5

# The frequency of 'perf' sampling.
PERF_FREQUENCY=9999

# --- Script variables ---
FLAMEGRAPH_DIR="/home/pg/code/FlameGraph"
OUTPUT_DIR = "output_perf_flamegraph"

def check_dependencies():
    """Check if required tools are available."""
    for cmd in ["perf", "git"]:
        if not shutil.which(cmd):
            print(f"Error: Command '{cmd}' not found. Please install it.")
            sys.exit(1)
    if not os.path.isdir(FLAMEGRAPH_DIR):
        raise FileNotFoundError("no FlameGraph repository")

def profile_postgres(pg_bin_dir, pg_name, pg_data_dir, conn_details, output_file):
    """Starts, profiles, and stops a PostgreSQL instance."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")

    print(f"--- Testing {pg_name} ---")

    # Ensure server is stopped before we start
    if subprocess.run([pg_ctl_path, "status", "-D", pg_data_dir], check=False).returncode == 0:
        print("Server is already running. Stopping it...")
        subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
        time.sleep(2)

    # Start the server
    print(f"Starting {pg_name} PostgreSQL server...")
    log_file = os.path.join(OUTPUT_DIR, f"{pg_name}.postgres_log")

    # Add port to the options if it's in the connection details
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    subprocess.run(
        [pg_ctl_path, "start",
         "-o", "--autovacuum=off",
         # "-o", "--debug_io_direct=data",
         "-D", pg_data_dir,
         "-l", log_file, "-o",
         start_options],
        check=True
    )

    # Wait for the server to be ready
    print("Waiting for server to accept connections...")
    for _ in range(10):
        try:
            conn = psycopg.connect(**conn_details)
            conn.close()
            print("Server started successfully.")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        print("Error: Server failed to start or become available.")
        sys.exit(1)

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
            cursor.execute("set enable_indexonlyscan=off;")
            cursor.execute("set enable_material=off;")
            cursor.execute("set enable_memoize=off;")
            cursor.execute("set enable_mergejoin=off;")
            cursor.execute("set enable_seqscan=off;")

            cursor.execute("set max_parallel_workers_per_gather = 0;")
            cursor.execute("create extension if not exists pg_prewarm;")
            cursor.execute("select pg_prewarm('pgbench_accounts');")
            cursor.execute("select pg_prewarm('pgbench_accounts_pkey');")
            cursor.execute("select pg_prewarm('pgbench_branches');")
            cursor.execute("select pg_prewarm('pgbench_branches_pkey');")
        print("Finished prewarming")

        perf_process = None
        perf_command = []
        if RUN_PERF:
            print(f"Starting perf on PID {backend_pid}...")
            perf_command = [
                "perf", "record",
                "-F", str(PERF_FREQUENCY),
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
            ]
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

        # Execute the query repeatedly in the same connection
        print(f"Executing the SQL query {QUERY_REPETITIONS} times...")
        random.seed(42)
        start_time = time.time()
        with conn.cursor() as cursor: # type: ignore
            for _ in range(QUERY_REPETITIONS):
                if SIMPLE_SELECT_PROFILING:
                    cursor.execute(query=SQL_QUERY,
                                   params=[random.randint(1, MAX_AID_VAL)],
                                   prepare=True)
                else:
                    cursor.execute(query=SQL_QUERY, prepare=True)
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
        # Ensure the connection is closed and the server is stopped
        if conn:
            conn.close()
        print("Stopping PostgreSQL server...")
        subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
        print("----------------------------------------")

    # Return string of perf command for flamegraph --subtitle arg
    return ' '.join(perf_command), total_time

def main():
    """Main execution flow."""
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

    if os.path.exists(OUTPUT_DIR):
        print(f"Removing previous output directory: {OUTPUT_DIR}")
        shutil.rmtree(OUTPUT_DIR)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"--- Profiling \"{SQL_QUERY}\" ---")

    stacks_file_master = os.path.join(OUTPUT_DIR, "master.stacks")
    stacks_file_patch = os.path.join(OUTPUT_DIR, "patch.stacks")
    folded_file_master = os.path.join(OUTPUT_DIR, "master.folded")
    folded_file_patch = os.path.join(OUTPUT_DIR, "patch.folded")
    svg_file_diff = os.path.join(OUTPUT_DIR, "diff.svg")
    svg_file_master = os.path.join(OUTPUT_DIR, "master_flamegraph.svg")
    svg_file_patch = os.path.join(OUTPUT_DIR, "patch_flamegraph.svg")

    # Profile both PostgreSQL versions
    perf_command_master, total_time_master = profile_postgres(MASTER_BIN, "master",
                                                              MASTER_DATA_DIR, MASTER_CONN_DETAILS,
                                                              stacks_file_master)
    perf_command_patch, total_time_patch = profile_postgres(PATCH_BIN, "patch",
                                                            PATCH_DATA_DIR, PATCH_CONN_DETAILS,
                                                            stacks_file_patch)

    print(f"Patch query loop took \033[1m{total_time_patch/total_time_master:.3f}x\033[0m as long as master")

    if not RUN_PERF:
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
                    "--title", "master, \"" + SQL_QUERY + "\"",
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
                    "--title", "patch, \"" + SQL_QUERY + "\"",
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
        "--title", "master versus patch, \"" + SQL_QUERY + "\"",
        "--subtitle", perf_command_master + ", " + perf_command_patch,
    ]

    p1 = subprocess.Popen(difffolded_cmd, stdout=subprocess.PIPE)
    with open(svg_file_diff, "w") as f_svg:
        subprocess.run(flamegraph_cmd, stdin=p1.stdout, stdout=f_svg, check=True)
    p1.stdout.close()

    print(f"Differential flame graph created: {svg_file_diff}")
    print("Done.")

if __name__ == "__main__":
    main()
