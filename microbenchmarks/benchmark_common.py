#!/usr/bin/env python3
"""
Shared module for PostgreSQL benchmark scripts.

Contains common configuration, query definitions, cache management,
tmpfs handling, and data loading utilities used by both prefetch_benchmark.py
and perf_flamegraph.py.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict

import psycopg

# --- Configuration ---

MASTER_BIN = "/mnt/nvme/postgresql/master/install_meson_rc/bin"
PATCH_BIN = "/mnt/nvme/postgresql/patch/install_meson_rc/bin"
MASTER_DATA_DIR = "/mnt/nvme/postgresql/master/data"
PATCH_DATA_DIR = "/mnt/nvme/postgresql/patch/data"
MASTER_SOURCE_DIR = "/mnt/nvme/postgresql/master/source"
PATCH_SOURCE_DIR = "/mnt/nvme/postgresql/patch/source"

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

# Expected row counts for data verification
EXPECTED_ORDERS = 50_000_000
EXPECTED_CUSTOMERS = 100_000
EXPECTED_PRODUCTS = 10_000
ROW_COUNT_TOLERANCE = 0.05  # 5%

# --- Query Definitions ---

QUERIES = OrderedDict([
    ("Q1", {
        "name": "Simple date range scan",
        "sql": """
            SELECT order_id, customer_id, amount
            FROM prefetch_orders
            WHERE order_date BETWEEN '2023-06-01' AND '2023-06-15'
            ORDER BY order_date
            LIMIT 50000
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("Q2", {
        "name": "JOIN Orders + Customers",
        "sql": """
            SELECT o.order_id, c.customer_name, o.amount, o.order_date
            FROM prefetch_orders o
            JOIN prefetch_customers c ON c.customer_id = o.customer_id
            WHERE o.customer_id BETWEEN 5000 AND 5500
              AND o.order_date BETWEEN '2023-06-01' AND '2023-06-30'
            ORDER BY o.order_date
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("Q4", {
        "name": "Aggregation with IN list",
        "sql": """
            SELECT order_date, sum(amount) AS total, count(*) AS orders
            FROM prefetch_orders
            WHERE customer_id IN (1000, 2000, 3000, 4000, 5000,
                                  6000, 7000, 8000, 9000, 10000)
              AND order_date BETWEEN '2023-01-01' AND '2023-12-31'
            GROUP BY order_date
            ORDER BY order_date
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("Q5", {
        "name": "Backwards scan with JOIN",
        "sql": """
            SELECT o.order_id, c.customer_name, o.amount, o.order_date
            FROM prefetch_orders o
            JOIN prefetch_customers c ON c.customer_id = o.customer_id
            WHERE o.customer_id BETWEEN 10000 AND 10500
              AND o.order_date BETWEEN '2023-09-01' AND '2023-09-30'
            ORDER BY o.customer_id DESC, o.order_date DESC
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
        "gucs": {"enable_sort": "off"},
    }),
    ("Q6", {
        "name": "Nested Loop with correlated subquery",
        "sql": """
            SELECT c.customer_name, (
              SELECT sum(amount) FROM prefetch_orders o
              WHERE o.customer_id = c.customer_id
            )
            FROM prefetch_customers c
            WHERE c.customer_id BETWEEN 1000 AND 1100
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("Q7", {
        "name": "LIMIT with large OFFSET",
        "sql": """
            SELECT * FROM prefetch_orders
            WHERE customer_id = 5000
            ORDER BY order_date
            OFFSET 400 LIMIT 100
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("Q8", {
        "name": "Semi-join with EXISTS",
        "sql": """
            SELECT c.customer_id, c.customer_name
            FROM prefetch_customers c
            WHERE EXISTS (
              SELECT 1 FROM prefetch_orders o
              WHERE o.customer_id = c.customer_id
                AND o.order_date BETWEEN '2023-07-01' AND '2023-07-31'
                AND o.amount > 900
            )
            AND c.region_id = 5
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("Q9", {
        "name": "Lateral join (Top N per group)",
        "sql": """
            SELECT c.customer_name, recent.*
            FROM prefetch_customers c,
            LATERAL (
              SELECT order_id, order_date, amount
              FROM prefetch_orders o
              WHERE o.customer_id = c.customer_id
              ORDER BY o.order_date DESC
              LIMIT 5
            ) recent
            WHERE c.customer_id BETWEEN 2000 AND 2100
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    # Adversarial queries
    ("A1", {
        "name": "Sequential heap access (adversarial)",
        "sql": """
            SELECT * FROM prefetch_sequential
            WHERE id BETWEEN 1 AND 200000
            ORDER BY id
        """,
        "evict": ["prefetch_sequential"],
        "prewarm_indexes": ["prefetch_sequential_idx"],
        "prewarm_tables": ["prefetch_sequential"],
        "setup_table": "prefetch_sequential",
    }),
    ("A3", {
        "name": "High cache hit ratio (adversarial)",
        "sql": """
            SELECT * FROM prefetch_orders
            WHERE customer_id = 1
              AND order_date BETWEEN '2023-01-01' AND '2023-12-31'
        """,
        # Special handling: no eviction, run query twice to warm cache first
        "evict": [],
        "prewarm_indexes": [],
        "prewarm_tables": [],
        "warmup_query": True,
    }),
    ("A4", {
        "name": "Early LIMIT termination (adversarial)",
        "sql": """
            SELECT * FROM prefetch_orders
            WHERE order_date BETWEEN '2023-01-01' AND '2023-12-31'
            ORDER BY order_date
            LIMIT 10
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("A5", {
        "name": "Sparse - 1 TID per block (adversarial)",
        "sql": """
            SELECT id, category FROM prefetch_sparse
            WHERE category BETWEEN 10 AND 20
        """,
        "evict": ["prefetch_sparse"],
        "prewarm_indexes": ["prefetch_sparse_cat_idx"],
        "prewarm_tables": ["prefetch_sparse"],
        "setup_table": "prefetch_sparse",
    }),
    ("A6", {
        "name": "NL inner with LIMIT 1 (adversarial)",
        "sql": """
            SELECT c.customer_name, (
              SELECT amount FROM prefetch_orders o
              WHERE o.customer_id = c.customer_id
              ORDER BY o.order_date DESC
              LIMIT 1
            )
            FROM prefetch_customers c
            WHERE c.customer_id BETWEEN 1000 AND 2000
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("A7", {
        "name": "Index Only Scan (adversarial)",
        "sql": """
            SELECT c.customer_id, c.customer_name
            FROM prefetch_customers c
            WHERE NOT EXISTS (
              SELECT 1 FROM prefetch_orders o
              WHERE o.customer_id = c.customer_id
                AND o.order_date BETWEEN '2023-12-01' AND '2023-12-31'
            )
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("A8", {
        "name": "LATERAL regression",
        "sql": """
            SELECT c.customer_id, o.order_id, o.order_date, o.amount
            FROM prefetch_customers c,
            LATERAL (
            SELECT order_id, order_date, amount
            FROM prefetch_orders
            WHERE customer_id = c.customer_id
            AND order_date BETWEEN '2023-05-01' AND '2023-05-20'

            ORDER BY order_date
            LIMIT 3
            ) o
            WHERE c.customer_id BETWEEN 42976 AND 43285
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("A9", {
        "name": "Found a bug in hold-onto-pin-after-all IoS fix",
        "sql": """
            SELECT order_date, count(*) as cnt
            FROM prefetch_orders
            WHERE order_date BETWEEN '2023-11-21' AND '2023-12-01'

            GROUP BY order_date
            ORDER BY order_date
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("A10", {
        "name": "regression: LATERAL, backwards",
        "sql": """
            SELECT c.customer_id, o.order_id, o.order_date, o.amount
            FROM prefetch_customers c,
            LATERAL (
            SELECT order_id, order_date, amount
            FROM prefetch_orders
            WHERE customer_id = c.customer_id
            AND order_date BETWEEN '2023-11-05' AND '2023-11-06'

            ORDER BY order_date DESC
            LIMIT 3
            ) o
            WHERE c.customer_id BETWEEN 67633 AND 68569
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("A11", {
        "name": "Merge Anti Join regression",
        "sql": """
            SELECT o.customer_id, o.order_date
            FROM prefetch_orders o
            WHERE o.order_date BETWEEN '2023-04-16' AND '2023-05-01'
            AND o.customer_id BETWEEN 46586 AND 47418
            AND NOT EXISTS (
            SELECT 1 FROM prefetch_customers c
            WHERE c.customer_id = o.customer_id
            AND c.region_id = 14
            )
            ORDER BY o.order_date
            LIMIT 37895
        """,
        "evict": ["prefetch_customers", "prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_customers", "prefetch_orders"],
    }),
    ("A12", {
        "name": "Merge Anti Join regression, index-only",
        "sql": """
            SELECT o.customer_id, o.order_date
            FROM prefetch_orders o
            WHERE o.order_date BETWEEN '2023-10-21' AND '2023-10-23'
            AND o.customer_id BETWEEN 15454 AND 23078
            AND NOT EXISTS (
            SELECT 1 FROM prefetch_customers c
            WHERE c.customer_id = o.customer_id
            AND c.region_id = 18
            )
        """,
        "evict": ["prefetch_orders", "prefetch_customers"],
        "prewarm_indexes": ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"],
        "prewarm_tables": ["prefetch_orders", "prefetch_customers"],
    }),
    ("A13", {
        "name": "New index-only scan aggregate regression",
        "sql": """
            SELECT order_date, count(*) as cnt
            FROM prefetch_orders
            WHERE order_date BETWEEN '2023-02-18' AND '2023-02-28'

            GROUP BY order_date
            ORDER BY order_date
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_date_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
    ("A14", {
        "name": "Regressed range-scan, index-only",
        "sql": """
            SELECT product_id
            FROM prefetch_orders
            WHERE product_id BETWEEN 3787 AND 6238
            ORDER BY product_id
            LIMIT 49763
        """,
        "evict": ["prefetch_orders"],
        "prewarm_indexes": ["prefetch_orders_prod_idx"],
        "prewarm_tables": ["prefetch_orders"],
    }),
])


# --- Data Loading SQL ---

DATA_LOADING_SQL = """
-- Create extensions
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;

-- Drop existing tables
DROP TABLE IF EXISTS prefetch_orders CASCADE;
DROP TABLE IF EXISTS prefetch_customers CASCADE;
DROP TABLE IF EXISTS prefetch_products CASCADE;
DROP TABLE IF EXISTS prefetch_sequential CASCADE;
DROP TABLE IF EXISTS prefetch_sparse CASCADE;

-- Main fact table: ~50M rows with low fillfactor
CREATE UNLOGGED TABLE prefetch_orders (
  order_id bigint,
  customer_id int,
  product_id int,
  order_date date,
  region_id int,
  amount numeric(10,2)
) WITH (fillfactor = 40);

-- Dimension tables
CREATE UNLOGGED TABLE prefetch_customers (
  customer_id int PRIMARY KEY,
  region_id int,
  customer_name text
);

CREATE UNLOGGED TABLE prefetch_products (
  product_id int PRIMARY KEY,
  category_id int,
  product_name text
);

-- Load customers (100K)
INSERT INTO prefetch_customers (customer_id, region_id, customer_name)
SELECT i, (i % 20) + 1, 'Customer_' || i
FROM generate_series(1, 100000) i;

-- Load products (10K)
INSERT INTO prefetch_products (product_id, category_id, product_name)
SELECT i, (i % 50) + 1, 'Product_' || i
FROM generate_series(1, 10000) i;

-- Set deterministic seed
SELECT setseed(0.5);

-- Load orders with controlled scatter pattern
INSERT INTO prefetch_orders (order_id, customer_id, product_id, order_date, region_id, amount)
SELECT
  row_number() over () as order_id,
  customer_id,
  product_id,
  order_date,
  (customer_id % 20) + 1 as region_id,
  (random() * 1000)::numeric(10,2) as amount
FROM (
  SELECT
    ((g.i - 1) % 100000) + 1 as customer_id,
    ((g.i - 1) % 10000) + 1 as product_id,
    '2023-01-01'::date + ((g.i - 1) % 730) as order_date
  FROM generate_series(1, 50000000) g(i)
  ORDER BY (g.i / 32) + (random() * 4 - 2)::int
) sub;

-- Create indexes
CREATE INDEX prefetch_orders_cust_date_idx
  ON prefetch_orders(customer_id, order_date)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_date_idx
  ON prefetch_orders(order_date)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_prod_idx
  ON prefetch_orders(product_id)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_id_idx
  ON prefetch_orders(order_id)
  WITH (deduplicate_items=off);

-- VACUUM FREEZE ANALYZE
VACUUM FREEZE ANALYZE prefetch_orders;
VACUUM FREEZE ANALYZE prefetch_customers;
VACUUM FREEZE ANALYZE prefetch_products;

-- Adversarial table: sequential heap access
CREATE UNLOGGED TABLE prefetch_sequential (
  id bigint,
  val1 int,
  val2 text
);
INSERT INTO prefetch_sequential
SELECT i, i % 1000, 'value_' || i
FROM generate_series(1, 500000) i;
CREATE INDEX prefetch_sequential_idx ON prefetch_sequential(id);
VACUUM ANALYZE prefetch_sequential;

-- Adversarial table: sparse (1 TID per block)
CREATE UNLOGGED TABLE prefetch_sparse (
  id bigint,
  category int,
  padding text
);
ALTER TABLE prefetch_sparse ALTER COLUMN padding SET STORAGE plain;
INSERT INTO prefetch_sparse
SELECT i, (i % 50) + 1, repeat('x', 4000)
FROM generate_series(1, 50000) i
ORDER BY random();
CREATE INDEX prefetch_sparse_cat_idx ON prefetch_sparse(category);
VACUUM ANALYZE prefetch_sparse;

CHECKPOINT;
"""


# --- Tmpfs Hugepages Functions ---

def setup_tmpfs_hugepages():
    """Create a tmpfs filesystem with huge=always and return the mount point."""
    tmpfs_mount = "/tmp/pg_tmpfs_hugepages"

    # Create mount point if it doesn't exist
    os.makedirs(tmpfs_mount, exist_ok=True)

    # Check if already mounted
    result = subprocess.run(
        ["mountpoint", "-q", tmpfs_mount],
        check=False
    )

    if result.returncode != 0:
        # Not mounted, so mount it
        print(f"Mounting tmpfs with huge=always at {tmpfs_mount}...")
        # Mount tmpfs with huge=always and 500MB size
        result = subprocess.run(
            ["sudo", "mount", "-t", "tmpfs", "-o", "huge=always,size=500M", "tmpfs_pg", tmpfs_mount],
            check=False
        )
        if result.returncode != 0:
            print(f"Error: Failed to mount tmpfs at {tmpfs_mount}")
            print("Make sure you have sudo privileges")
            sys.exit(1)
        print(f"Successfully mounted tmpfs at {tmpfs_mount}")
    else:
        print(f"tmpfs already mounted at {tmpfs_mount}")

    return tmpfs_mount


def copy_binaries_to_tmpfs(src_bin_dir, tmpfs_mount, version_name):
    """Copy PostgreSQL binaries from src_bin_dir to tmpfs and return new bin path."""
    dest_bin_dir = os.path.join(tmpfs_mount, version_name)

    print(f"Copying {version_name} binaries from {src_bin_dir} to {dest_bin_dir}...")

    # Remove existing directory if it exists
    if os.path.exists(dest_bin_dir):
        shutil.rmtree(dest_bin_dir)

    # Copy the entire bin directory
    shutil.copytree(src_bin_dir, dest_bin_dir, symlinks=True)

    print(f"Successfully copied binaries to {dest_bin_dir}")
    return dest_bin_dir


def cleanup_tmpfs(tmpfs_mount):
    """Unmount the tmpfs filesystem."""
    print(f"Unmounting tmpfs at {tmpfs_mount}...")
    result = subprocess.run(
        ["sudo", "umount", tmpfs_mount],
        check=False
    )
    if result.returncode != 0:
        print(f"Warning: Failed to unmount tmpfs at {tmpfs_mount}")
    else:
        print(f"Successfully unmounted tmpfs")

    # Try to remove the mount point directory
    try:
        os.rmdir(tmpfs_mount)
    except OSError:
        pass


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
                    print(f"Warning: Failed to evict {rel}: {e}")
                    break


def prewarm_relations(conn, relations):
    """Prewarm relations into PostgreSQL buffer cache."""
    with conn.cursor() as cur:
        for rel in relations:
            try:
                cur.execute(f"SELECT pg_prewarm('{rel}')")
            except Exception as e:
                print(f"Warning: Failed to prewarm {rel}: {e}")


def set_gucs(conn, gucs, is_master=False, prefetch_setting=None):
    """Set GUCs for query execution."""
    with conn.cursor() as cur:
        # Base GUCs for all configurations
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")

        # Set prefetch GUC only on patch (not master)
        if not is_master and prefetch_setting is not None:
            cur.execute(f"SET enable_indexscan_prefetch = {prefetch_setting}")

        # Query-specific GUCs
        for guc, value in gucs.items():
            cur.execute(f"SET {guc} = {value}")


def reset_gucs(conn, gucs):
    """Reset query-specific GUCs to defaults."""
    with conn.cursor() as cur:
        for guc in gucs:
            cur.execute(f"RESET {guc}")


# --- Data Verification and Loading ---

def verify_data(conn_details, skip_load=False):
    """
    Verify that tables exist with expected row counts.
    Returns True if data is valid, False if reload is needed.
    """
    if skip_load:
        print("Skipping data verification (--skip-load)")
        return True

    try:
        conn = psycopg.connect(**conn_details)

        # Check if main table exists
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class WHERE relname = 'prefetch_orders' AND relkind = 'r'
                )
            """)
            if not cur.fetchone()[0]:
                print("Table prefetch_orders does not exist")
                conn.close()
                return False

        # Check row counts
        checks = [
            ("prefetch_orders", EXPECTED_ORDERS),
            ("prefetch_customers", EXPECTED_CUSTOMERS),
            ("prefetch_products", EXPECTED_PRODUCTS),
        ]

        for table, expected in checks:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {table}")
                actual = cur.fetchone()[0]
                tolerance = expected * ROW_COUNT_TOLERANCE
                if abs(actual - expected) > tolerance:
                    print(f"Table {table}: expected ~{expected:,} rows, got {actual:,}")
                    conn.close()
                    return False
                print(f"Table {table}: {actual:,} rows (expected ~{expected:,}) ✓")

        # Check adversarial tables exist
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class WHERE relname = 'prefetch_sequential' AND relkind = 'r'
                )
            """)
            if not cur.fetchone()[0]:
                print("Adversarial table prefetch_sequential does not exist")
                conn.close()
                return False

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class WHERE relname = 'prefetch_sparse' AND relkind = 'r'
                )
            """)
            if not cur.fetchone()[0]:
                print("Adversarial table prefetch_sparse does not exist")
                conn.close()
                return False

        conn.close()
        return True

    except Exception as e:
        print(f"Error verifying data: {e}")
        return False


def load_data(conn_details):
    """Load benchmark data into the database."""
    print("\n" + "=" * 50)
    print("Loading benchmark data...")
    print("This will take several minutes for 50M rows.")
    print("=" * 50 + "\n")

    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    # Parse SQL into individual statements, handling comments properly
    statements = []
    current_stmt = []
    for line in DATA_LOADING_SQL.split('\n'):
        stripped = line.strip()
        # Skip pure comment lines and empty lines when building statements
        if stripped.startswith('--') or not stripped:
            continue
        current_stmt.append(line)
        if stripped.endswith(';'):
            statements.append('\n'.join(current_stmt))
            current_stmt = []

    for statement in statements:
        statement = statement.strip()
        if not statement:
            continue
        try:
            # Print progress for long operations BEFORE executing
            if 'INSERT INTO prefetch_orders' in statement and 'generate_series(1, 50000000)' in statement:
                print("Loading 50M orders... (this takes a while)")
            elif 'CREATE INDEX' in statement:
                idx_match = re.search(r'CREATE INDEX (\S+)', statement)
                idx_name = idx_match.group(1) if idx_match else "index"
                print(f"Creating index {idx_name}...")
            elif 'VACUUM' in statement:
                print(f"Running {statement.split()[0]} {statement.split()[1] if len(statement.split()) > 1 else ''}...")

            with conn.cursor() as cur:
                cur.execute(statement)

        except Exception as e:
            print(f"Error executing: {statement[:80]}...")
            print(f"Error: {e}")
            conn.close()
            sys.exit(1)

    conn.close()
    print("Data loading complete.")


def extract_execution_time(explain_output):
    """Extract execution time from EXPLAIN ANALYZE output."""
    for line in explain_output:
        match = re.search(r'Execution Time: ([\d.]+) ms', line[0])
        if match:
            return float(match.group(1))
    return None
