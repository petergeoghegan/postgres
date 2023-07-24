#!/usr/bin/env python3

import os
import psycopg
import shutil
import random
import signal
import subprocess
import sys
import time

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
    "port": 5555 # IMPORTANT: Ensure the two versions run on different ports
}
PATCH_CONN_DETAILS = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp", # Use socket for local connections
    "port": 5432
}

# The SQL query you want to profile.
SQL_QUERY="select * from pgbench_accounts where aid = %s;"

# Number of times to run the query to ensure it's captured by perf.
QUERY_REPETITIONS=1_000_000

# The frequency of 'perf' sampling.
PERF_FREQUENCY=9999

# The user that the PostgreSQL server runs as.
PG_USER = "pg"

# --- Script variables ---
FLAMEGRAPH_DIR="/home/pg/code/FlameGraph"
OUTPUT_DIR = "output_perf_flamegraph"

def check_dependencies():
    """Check if required command-line tools are available."""
    for cmd in ["perf", "git"]:
        if not shutil.which(cmd):
            print(f"Error: Command '{cmd}' not found. Please install it.")
            sys.exit(1)

def clone_flamegraph():
    """Clone the FlameGraph repository if it doesn't exist."""
    if not os.path.isdir(FLAMEGRAPH_DIR):
        raise Exception("no FlameGraph repository")
    else:
        print("FlameGraph repository already exists.")

def profile_postgres(pg_bin_dir, pg_name, pg_data_dir, conn_details, output_file):
    """Starts, profiles, and stops a PostgreSQL instance."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")

    print(f"--- Profiling with {os.path.basename(pg_bin_dir)} ---")

    # Ensure server is stopped before we start
    if subprocess.run([pg_ctl_path, "status", "-D", pg_data_dir]).returncode == 0:
        print("Server is already running. Stopping it...")
        subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
        time.sleep(2)

    # Start the server
    print("Starting PostgreSQL server...")
    log_file = os.path.join(OUTPUT_DIR, f"pg_{os.path.basename(pg_bin_dir)}.log")

    # Add port to the options if it's in the connection details
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    subprocess.run(
        [pg_ctl_path, "start",
         "-o", "--autovacuum=off",
         "-o", "--debug_io_direct=data",
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

        # Start perf in the background, targeting the specific backend PID.
        # It will run until we explicitly stop it.
        print(f"Starting perf on PID {backend_pid}...")
        perf_command = [
            "perf", "record",
            "-F", str(PERF_FREQUENCY),
            "-e", "cache-misses",
            # "-p", str(backend_pid),
            "-o",  OUTPUT_DIR + "/" + pg_name,
            "-g"
        ]
        perf_process = subprocess.Popen(perf_command)

        # Give perf a moment to initialize before starting the workload
        time.sleep(1)

        # Execute the query repeatedly in the same connection
        print(f"Executing the SQL query {QUERY_REPETITIONS} times...")
        start_time = time.time()
        with conn.cursor() as cursor:
            cursor.execute("set enable_seqscan=off;")
            cursor.execute("set enable_bitmapscan=off;")
            cursor.execute("create extension if not exists pg_prewarm;")
            cursor.execute("select pg_prewarm('pgbench_accounts');")
            cursor.execute("select pg_prewarm('pgbench_accounts_pkey');")
            for i in range(QUERY_REPETITIONS):
                cursor.execute(query=SQL_QUERY, params=[random.randint(1,100_000)], prepare=True)
        end_time = time.time()
        print(f"Query loop finished in {end_time - start_time:.2f} seconds.")

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


def main():
    """Main execution flow."""
    check_dependencies()
    clone_flamegraph()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    stacks_file1 = os.path.join(OUTPUT_DIR, "out.stacks1")
    stacks_file2 = os.path.join(OUTPUT_DIR, "out.stacks2")
    folded_file1 = os.path.join(OUTPUT_DIR, "out.folded1")
    folded_file2 = os.path.join(OUTPUT_DIR, "out.folded2")
    svg_diff_file = os.path.join(OUTPUT_DIR, "diff.svg")
    svg_master_file = os.path.join(OUTPUT_DIR, "master_flamegraph.svg")
    svg_patch_file = os.path.join(OUTPUT_DIR, "patch_flamegraph.svg")

    # Profile both PostgreSQL versions
    profile_postgres(MASTER_BIN, "master", MASTER_DATA_DIR, MASTER_CONN_DETAILS, stacks_file1)
    profile_postgres(PATCH_BIN, "patch", PATCH_DATA_DIR, PATCH_CONN_DETAILS, stacks_file2)

    # --- Generate Flame Graphs ---
    print("--- Generating Flame Graphs ---")

    # Fold stack traces
    print("Folding stack traces...")
    with open(folded_file1, "w") as f1, open(folded_file2, "w") as f2:
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "stackcollapse-perf.pl"), stacks_file1],
            stdout=f1, check=True
        )
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "stackcollapse-perf.pl"), stacks_file2],
            stdout=f2, check=True
        )

    # Create individual flame graph for V1
    print("Creating individual flame graph for master...")
    with open(svg_master_file, "w") as f_svg:
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl"), folded_file1],
            stdout=f_svg, check=True
        )
    print(f"V1 flame graph created: {svg_master_file}")

    # Create individual flame graph for V2
    print("Creating individual flame graph for patch...")
    with open(svg_patch_file, "w") as f_svg:
        subprocess.run(
            [os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl"), folded_file2],
            stdout=f_svg, check=True
        )
    print(f"V2 flame graph created: {svg_patch_file}")

    # Create the differential SVG
    print("Creating the differential flame graph...")
    difffolded_cmd = [os.path.join(FLAMEGRAPH_DIR, "difffolded.pl"), folded_file1, folded_file2]
    flamegraph_cmd = [os.path.join(FLAMEGRAPH_DIR, "flamegraph.pl")]

    p1 = subprocess.Popen(difffolded_cmd, stdout=subprocess.PIPE)
    with open(svg_diff_file, "w") as f_svg:
        subprocess.run(flamegraph_cmd, stdin=p1.stdout, stdout=f_svg, check=True)
    p1.stdout.close()

    print(f"Differential flame graph created: {svg_diff_file}")
    print("Done.")

if __name__ == "__main__":
    main()
