#!/usr/bin/env python3
"""
Prefetch benchmark script for PostgreSQL index scan prefetching.

Compares query performance across three configurations:
1. master (no prefetching, GUC not available)
2. patch with enable_indexscan_prefetch=off
3. patch with enable_indexscan_prefetch=on

Usage:
    ./prefetch_benchmark.py                    # Run all queries, uncached, 3 runs
    ./prefetch_benchmark.py --cached           # Run with data prewarmed
    ./prefetch_benchmark.py --queries Q1,Q2    # Run specific queries
    ./prefetch_benchmark.py --runs 5           # 5 runs per query
    ./prefetch_benchmark.py --skip-load        # Skip data loading verification
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from statistics import mean

import psycopg

from benchmark_common import (
    QUERIES,
    MASTER_BIN, PATCH_BIN,
    MASTER_DATA_DIR, PATCH_DATA_DIR,
    MASTER_SOURCE_DIR, PATCH_SOURCE_DIR,
    MASTER_CONN, PATCH_CONN,
    DATA_LOADING_SQL,
    EXPECTED_ORDERS, EXPECTED_CUSTOMERS, EXPECTED_PRODUCTS, ROW_COUNT_TOLERANCE,
    verify_data, load_data,
    clear_os_cache, evict_relations, prewarm_relations,
    set_gucs, reset_gucs,
    setup_tmpfs_hugepages, copy_binaries_to_tmpfs, cleanup_tmpfs,
)

# Output directory for results
OUTPUT_DIR = "prefetch_results"

# CPU pinning settings
BENCHMARK_CPU = 14

# --- Stress-test query generation probabilities ---
# These control the likelihood of various query features in randomly generated queries.
# Tune these to focus on patterns most likely to expose regressions.

STRESS_PROB_LATERAL_JOIN = 0.15        # Use LATERAL subquery (top-N per group)
STRESS_PROB_ANTI_JOIN = 0.10           # Use NOT EXISTS anti-join
STRESS_PROB_SEMI_JOIN = 0.15           # Use EXISTS semi-join
STRESS_PROB_CORRELATED_SUBQUERY = 0.10 # Correlated subquery in SELECT clause
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

# Stress-test configuration
STRESS_QUERIES_PER_BATCH = 10          # Number of queries to generate per iteration
STRESS_REGRESSION_THRESHOLD = 1.06     # 6% slower = regression
STRESS_MIN_QUERY_MS = 2.5             # Discard queries slower than this (too noisy)

# QUERIES is imported from benchmark_common


# --- Readstream Test Queries ---
# Based on tomas-weird-issue-readstream.sql

READSTREAM_QUERIES = OrderedDict([
    ("RS1", {
        "name": "Readstream benefits (forward scan)",
        "sql": """
            SELECT * FROM t_readstream
            WHERE a BETWEEN 16150 AND 4540437
            ORDER BY a ASC
        """,
        "evict": ["t_readstream"],
        "prewarm_indexes": ["idx_readstream"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
    ("RS2", {
        "name": "Tupdistance regression (backward scan)",
        "sql": """
            SELECT * FROM t_tupdistance_new_regress
            WHERE a BETWEEN 9401 AND 2271544
            ORDER BY a DESC
        """,
        "evict": ["t_tupdistance_new_regress"],
        "prewarm_indexes": ["t_tupdistance_new_regress_idx"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
    ("RS3", {
        "name": "Remaining regression (forward scan, negative values)",
        "sql": """
            SELECT * FROM t_remaining_regression
            WHERE a BETWEEN -2281232 AND -19089
            ORDER BY a ASC
        """,
        "evict": ["t_remaining_regression"],
        "prewarm_indexes": ["t_remaining_regression_idx"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
])


# --- Random Backwards Test Queries ---
# Based on random_backwards_weird.sql

RANDOM_BACKWARDS_QUERIES = OrderedDict([
    ("RB1", {
        "name": "Sequential table forward scan",
        "sql": """
            SELECT * FROM t
            WHERE a BETWEEN 16336 AND 49103
            ORDER BY a
        """,
        "evict": ["t"],
        "prewarm_indexes": ["t_pk"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
    ("RB2", {
        "name": "Sequential table backward scan",
        "sql": """
            SELECT * FROM t
            WHERE a BETWEEN 16336 AND 49103
            ORDER BY a DESC
        """,
        "evict": ["t"],
        "prewarm_indexes": ["t_pk"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
    ("RB3", {
        "name": "Randomized table forward scan",
        "sql": """
            SELECT * FROM t_randomized
            WHERE a BETWEEN 16336 AND 49103
            ORDER BY a
        """,
        "evict": ["t_randomized"],
        "prewarm_indexes": ["t_randomized_pk"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
    ("RB4", {
        "name": "Randomized table backward scan",
        "sql": """
            SELECT * FROM t_randomized
            WHERE a BETWEEN 16336 AND 49103
            ORDER BY a DESC
        """,
        "evict": ["t_randomized"],
        "prewarm_indexes": ["t_randomized_pk"],
        "prewarm_tables": [],
        "gucs": {
            "enable_bitmapscan": "off",
            "enable_seqscan": "off",
        },
    }),
])


# DATA_LOADING_SQL is imported from benchmark_common


# --- Readstream Test Data Loading SQL ---
# Based on tomas-weird-issue-readstream.sql

READSTREAM_DATA_SQL = """
-- Create extensions
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;

-- Drop existing tables
DROP TABLE IF EXISTS t_readstream CASCADE;
DROP TABLE IF EXISTS t_tupdistance_new_regress CASCADE;
DROP TABLE IF EXISTS t_remaining_regression CASCADE;

-- Table 1: t_readstream (5M rows x 2 = 10M rows)
CREATE UNLOGGED TABLE t_readstream (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.1234567890123456);
INSERT INTO t_readstream
SELECT
  1 * a,
  b
FROM (
  SELECT r, a, b, generate_series(0, 2 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 5000000) s(i)
      ORDER BY
        (i + 16 * (random() - 0.5))) foo) bar) baz
ORDER BY
  ((r * 2 + p) + 8 * (random() - 0.5));
CREATE INDEX idx_readstream ON t_readstream(a ASC) WITH (deduplicate_items=false);
VACUUM (ANALYZE, FREEZE) t_readstream;

-- Table 2: t_tupdistance_new_regress (2.5M rows x 4 = 10M rows)
CREATE UNLOGGED TABLE t_tupdistance_new_regress (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.2345678901234567);
INSERT INTO t_tupdistance_new_regress
SELECT 1 * a, b
FROM (
  SELECT
    r,
    a,
    b,
    generate_series(0, 4 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 2500000) s(i)
      ORDER BY
        (i + 0 * (random() - 0.5))) foo) bar) baz
ORDER BY
  ((r * 4 + p) + 8 * (random() - 0.5));
CREATE INDEX t_tupdistance_new_regress_idx ON t_tupdistance_new_regress(a DESC) WITH (deduplicate_items = false);

-- Table 3: t_remaining_regression (2.5M rows x 4 = 10M rows, negative values)
CREATE UNLOGGED TABLE t_remaining_regression (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.8152497610420479);
INSERT INTO t_remaining_regression SELECT -1 * a, b
FROM (
  SELECT r, a, b, generate_series(0, 4 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 2500000) s(i)
      ORDER BY
        (i + 0 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 4 + p) + 8 * (random() - 0.5));
CREATE INDEX t_remaining_regression_idx ON t_remaining_regression(a ASC) WITH (deduplicate_items = false);

VACUUM ANALYZE;
CHECKPOINT;
"""


# --- Random Backwards Test Data Loading SQL ---
# Based on random_backwards_weird.sql

RANDOM_BACKWARDS_DATA_SQL = """
-- Create extensions
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
CREATE EXTENSION IF NOT EXISTS pg_buffercache;

-- Drop existing tables
DROP TABLE IF EXISTS t CASCADE;
DROP TABLE IF EXISTS t_randomized CASCADE;

SET synchronize_seqscans = off;

-- Table 1: t (sequential layout, 312500 x 32 = 10M rows)
CREATE UNLOGGED TABLE t (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.3456789012345678);
INSERT INTO t
SELECT a, b
FROM (SELECT
    r,
    a,
    b,
    generate_series(0, 32 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 312500) s(i)
      ORDER BY
        (i + 1 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 32 + p) + 8 * (random() - 0.5));
CREATE INDEX t_pk ON t(a ASC) WITH (deduplicate_items=off);

-- Table 2: t_randomized (clustered by hash for random physical layout)
CREATE UNLOGGED TABLE t_randomized (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.4567890123456789);
INSERT INTO t_randomized
SELECT a, b
FROM (SELECT
    r,
    a,
    b,
    generate_series(0, 32 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 312500) s(i)
      ORDER BY
        (i + 1 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 32 + p) + 8 * (random() - 0.5));
CREATE INDEX t_randomized_pk ON t_randomized(a ASC) WITH (deduplicate_items=off);
CREATE INDEX randomizer ON t_randomized (hashint8(a));
CLUSTER t_randomized USING randomizer;

VACUUM FREEZE;
ANALYZE;
CHECKPOINT;
"""


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
    %(prog)s --runs 5                # 5 runs per query
    %(prog)s --skip-load             # Skip data loading verification
        """
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help="Run in cached mode (prewarm all relations instead of just indexes)"
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help="Comma-separated list of queries to run (e.g., Q1,Q2,A1). Default: all"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of runs per query (default: 3)"
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        dest="skip_load",
        help="Skip data loading verification (assume tables exist with correct data)"
    )
    parser.add_argument(
        "--benchmark-cpu",
        type=int,
        default=BENCHMARK_CPU,
        help=f"CPU core to pin PostgreSQL backend to (default: {BENCHMARK_CPU})"
    )
    parser.add_argument(
        "--topn",
        type=int,
        default=10,
        help="Number of top improvements/regressions to show (default: 10)"
    )
    prefetch_group = parser.add_mutually_exclusive_group()
    prefetch_group.add_argument(
        "--prefetch-only",
        action="store_true",
        dest="prefetch_only",
        help="Only test patch with prefetching enabled (skip prefetch=off)"
    )
    prefetch_group.add_argument(
        "--prefetch-disabled",
        action="store_true",
        dest="prefetch_disabled",
        help="Only test patch with prefetching disabled (skip prefetch=on)"
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        dest="stress_test",
        help="Run stress test mode: randomly generate queries to find regressions"
    )
    parser.add_argument(
        "--min-query-ms",
        type=float,
        default=STRESS_MIN_QUERY_MS,
        dest="min_query_ms",
        help=f"Minimum query duration in ms for stress test (default: {STRESS_MIN_QUERY_MS}). "
             "Queries slower than this on master are discarded as too noisy."
    )
    parser.add_argument(
        "--no-tmpfs-hugepages",
        action="store_true",
        help="Disable tmpfs with huge=always (enabled by default)"
    )
    parser.add_argument(
        "--readstream-tests",
        action="store_true",
        dest="readstream_tests",
        help="Run readstream benchmark tests (loads data if needed)"
    )
    parser.add_argument(
        "--random-backwards-tests",
        action="store_true",
        dest="random_backwards_tests",
        help="Run random backwards benchmark tests (loads data if needed)"
    )
    return parser.parse_args()


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
    Returns a query definition dict compatible with the QUERIES format.
    """
    # Decide query type based on probabilities
    # These are mutually exclusive special patterns
    use_lateral = random.random() < STRESS_PROB_LATERAL_JOIN
    use_anti_join = not use_lateral and random.random() < STRESS_PROB_ANTI_JOIN
    use_semi_join = not use_lateral and not use_anti_join and random.random() < STRESS_PROB_SEMI_JOIN
    use_correlated = not use_lateral and not use_anti_join and not use_semi_join and random.random() < STRESS_PROB_CORRELATED_SUBQUERY
    use_aggregate = not use_lateral and random.random() < STRESS_PROB_AGGREGATE

    # Independent features
    use_filter_qual = random.random() < STRESS_PROB_FILTER_QUAL
    use_order_by = random.random() < STRESS_PROB_ORDER_BY
    use_limit = use_order_by and random.random() < STRESS_PROB_LIMIT
    use_backwards = use_order_by and random.random() < STRESS_PROB_BACKWARDS_SCAN
    use_in_list = random.random() < STRESS_PROB_IN_LIST
    use_index_only = random.random() < STRESS_PROB_INDEX_ONLY
    use_multi_join = random.random() < STRESS_PROB_MULTI_TABLE_JOIN

    # Generate date range (used in most queries)
    date_start, date_end = random_date_range()
    cust_start, cust_end = random_customer_range()
    prod_start, prod_end = random_product_range()

    # Build query based on type
    evict = ["prefetch_orders"]
    prewarm_indexes = []
    prewarm_tables = ["prefetch_orders"]
    query_features = []

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
                  AND order_date BETWEEN '{date_start}' AND '{date_end}'
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
            WHERE o.order_date BETWEEN '{date_start}' AND '{date_end}'
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
            sql = sql.rstrip() + f"\n            ORDER BY o.order_date {order_dir}"
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
            WHERE o.order_date BETWEEN '{date_start}' AND '{date_end}'
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
            sql = sql.rstrip() + f"\n            ORDER BY o.order_date {order_dir}"
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
            WHERE o.order_date BETWEEN '{date_start}' AND '{date_end}'
              AND {cust_cond}
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            sql = sql.rstrip() + f"\n            ORDER BY o.order_date {order_dir}"
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
            WHERE order_date BETWEEN '{date_start}' AND '{date_end}'
              {filter_clause}
            GROUP BY order_date
        """
        prewarm_indexes = ["prefetch_orders_date_idx"]

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
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
                  AND o.order_date BETWEEN '{date_start}' AND '{date_end}'
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
                  AND o.order_date BETWEEN '{date_start}' AND '{date_end}'
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
                WHERE o.order_date BETWEEN '{date_start}' AND '{date_end}'
                  AND {cust_cond}
                  {filter_clause}
            """
            evict.extend(["prefetch_customers", "prefetch_products"])
            prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey", "prefetch_products_pkey"]
            prewarm_tables.extend(["prefetch_customers", "prefetch_products"])

        if use_order_by:
            order_dir = "DESC" if use_backwards else ""
            sql = sql.rstrip() + f"\n                ORDER BY o.order_date {order_dir}"
            if use_limit:
                sql += f"\n                LIMIT {random_limit()}"

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
                WHERE order_date BETWEEN '{date_start}' AND '{date_end}'
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
                  AND order_date BETWEEN '{date_start}' AND '{date_end}'
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
            order_col = "order_date" if index_choice in ['date', 'cust_date'] else "product_id"
            order_dir = "DESC" if use_backwards else ""
            sql = sql.rstrip() + f"\n                ORDER BY {order_col} {order_dir}"
            if use_limit:
                sql += f"\n                LIMIT {random_limit()}"

    # Add backwards scan to features if used
    if use_backwards:
        query_features.append("backwards")

    # Build name from features
    name = f"Stress #{query_num}: {', '.join(query_features)}"

    return {
        "name": name,
        "sql": sql,
        "evict": list(set(evict)),  # Remove duplicates
        "prewarm_indexes": prewarm_indexes,
        "prewarm_tables": list(set(prewarm_tables)),
    }


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


# setup_tmpfs_hugepages, copy_binaries_to_tmpfs, cleanup_tmpfs are imported from benchmark_common


def start_server(pg_bin_dir, pg_name, pg_data_dir, conn_details):
    """Start a PostgreSQL server and wait for it to be ready."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    log_file = os.path.join(OUTPUT_DIR, f"{pg_name}.postgres_log")

    # Ensure server is stopped before we start
    result = subprocess.run(
        [pg_ctl_path, "status", "-D", pg_data_dir],
        capture_output=True,
        check=False
    )
    if result.returncode == 0:
        print(f"{pg_name}: Server is already running. Stopping it...")
        subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
        time.sleep(2)

    # Start the server
    print(f"Starting {pg_name} PostgreSQL server (port {conn_details.get('port', 'default')})...")
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    result = subprocess.run(
        [pg_ctl_path, "start",
         "-o", "--autovacuum=off",
         "-D", pg_data_dir,
         "-l", log_file,
         "-o", start_options],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"Error: Failed to start {pg_name} server")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)

    # Wait for the server to be ready
    print(f"Waiting for {pg_name} server to accept connections...")
    for attempt in range(15):
        try:
            conn = psycopg.connect(**conn_details, connect_timeout=2)
            conn.close()
            print(f"{pg_name} server started successfully.")
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


# verify_data and load_data are imported from benchmark_common


def verify_readstream_data(conn_details):
    """
    Verify that readstream test tables exist.
    Returns True if data is valid, False if reload is needed.
    """
    tables = ['t_readstream', 't_tupdistance_new_regress', 't_remaining_regression']
    try:
        conn = psycopg.connect(**conn_details)
        for table in tables:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'r'
                    )
                """, (table,))
                if not cur.fetchone()[0]:
                    print(f"Table {table} does not exist")
                    conn.close()
                    return False
            print(f"Table {table} exists ✓")
        conn.close()
        return True
    except Exception as e:
        print(f"Error verifying readstream data: {e}")
        return False


def load_readstream_data(conn_details):
    """Load readstream benchmark data into the database."""
    print("\n" + "=" * 50)
    print("Loading readstream benchmark data...")
    print("This will take several minutes for 30M rows.")
    print("=" * 50 + "\n")

    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    # Parse SQL into individual statements
    statements = []
    current_stmt = []
    for line in READSTREAM_DATA_SQL.split('\n'):
        stripped = line.strip()
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
            # Print progress for long operations
            if 'INSERT INTO t_readstream' in statement:
                print("Loading t_readstream (10M rows)...")
            elif 'INSERT INTO t_tupdistance_new_regress' in statement:
                print("Loading t_tupdistance_new_regress (10M rows)...")
            elif 'INSERT INTO t_remaining_regression' in statement:
                print("Loading t_remaining_regression (10M rows)...")
            elif 'CREATE INDEX' in statement:
                idx_match = re.search(r'CREATE INDEX (\S+)', statement)
                idx_name = idx_match.group(1) if idx_match else "index"
                print(f"Creating index {idx_name}...")

            with conn.cursor() as cur:
                cur.execute(statement)

        except Exception as e:
            print(f"Error executing: {statement[:80]}...")
            print(f"Error: {e}")
            conn.close()
            sys.exit(1)

    conn.close()
    print("Readstream data loading complete.")


def verify_random_backwards_data(conn_details):
    """
    Verify that random backwards test tables exist.
    Returns True if data is valid, False if reload is needed.
    """
    tables = ['t', 't_randomized']
    try:
        conn = psycopg.connect(**conn_details)
        for table in tables:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'r'
                    )
                """, (table,))
                if not cur.fetchone()[0]:
                    print(f"Table {table} does not exist")
                    conn.close()
                    return False
            print(f"Table {table} exists ✓")
        conn.close()
        return True
    except Exception as e:
        print(f"Error verifying random backwards data: {e}")
        return False


def load_random_backwards_data(conn_details):
    """Load random backwards benchmark data into the database."""
    print("\n" + "=" * 50)
    print("Loading random backwards benchmark data...")
    print("This will take several minutes for 20M rows.")
    print("=" * 50 + "\n")

    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    # Parse SQL into individual statements
    statements = []
    current_stmt = []
    for line in RANDOM_BACKWARDS_DATA_SQL.split('\n'):
        stripped = line.strip()
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
            # Print progress for long operations
            if 'INSERT INTO t_randomized' in statement:
                print("Loading t_randomized (10M rows)...")
            elif 'INSERT INTO t' in statement and 'INSERT INTO t_randomized' not in statement:
                print("Loading t (10M rows)...")
            elif 'CREATE INDEX' in statement:
                idx_match = re.search(r'CREATE INDEX (\S+)', statement)
                idx_name = idx_match.group(1) if idx_match else "index"
                print(f"Creating index {idx_name}...")
            elif 'CLUSTER' in statement:
                print("Clustering t_randomized by hash (randomizing physical order)...")

            with conn.cursor() as cur:
                cur.execute(statement)

        except Exception as e:
            print(f"Error executing: {statement[:80]}...")
            print(f"Error: {e}")
            conn.close()
            sys.exit(1)

    conn.close()
    print("Random backwards data loading complete.")


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


# clear_os_cache, evict_relations, prewarm_relations, set_gucs, reset_gucs
# are imported from benchmark_common


def extract_execution_time(explain_output):
    """Extract execution time from EXPLAIN ANALYZE output."""
    for line in explain_output:
        match = re.search(r'Execution Time: ([\d.]+) ms', line[0])
        if match:
            return float(match.group(1))
    return None


def pin_backend(pid, cpu):
    """Pin a backend process to a specific CPU."""
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


def run_query(conn, query_def, cached_mode, is_master, prefetch_setting, benchmark_cpu):
    """
    Run a single query with proper cache preparation.
    Returns (execution_time_ms, explain_output_str) tuple.
    """
    # Cache preparation
    if cached_mode:
        # Prewarm everything
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        prewarm_relations(conn, query_def.get("prewarm_tables", []))
    else:
        # Uncached: evict heap, prewarm indexes, clear OS cache
        evict_relations(conn, query_def.get("evict", []))
        prewarm_relations(conn, query_def.get("prewarm_indexes", []))
        clear_os_cache()

    # Set GUCs
    gucs = query_def.get("gucs", {})
    set_gucs(conn, gucs, is_master=is_master, prefetch_setting=prefetch_setting)

    # Special handling for warmup queries (A3)
    if query_def.get("warmup_query") and not cached_mode:
        with conn.cursor() as cur:
            cur.execute(query_def["sql"])
            cur.execute(query_def["sql"])

    # Execute with EXPLAIN ANALYZE
    sql = query_def["sql"].strip()
    explain_sql = f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) {sql}"

    with conn.cursor() as cur:
        cur.execute(explain_sql)
        result = cur.fetchall()

    # Reset query-specific GUCs
    if gucs:
        reset_gucs(conn, gucs)

    # Format EXPLAIN output as string
    explain_output = "\n".join(row[0] for row in result)

    # Extract execution time
    exec_time = extract_execution_time(result)
    if exec_time is None:
        print("Warning: Could not extract execution time from EXPLAIN output")
        return None, explain_output

    return exec_time, explain_output


def run_benchmark(args):
    """Run the benchmark."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = MASTER_BIN
    patch_bin = PATCH_BIN

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(MASTER_BIN, tmpfs_mount, "master")
        patch_bin = copy_binaries_to_tmpfs(PATCH_BIN, tmpfs_mount, "patch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  master: {master_bin}")
        print(f"  patch: {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    # Parse query selection
    if args.queries:
        selected_queries = [q.strip().upper() for q in args.queries.split(",")]
        for q in selected_queries:
            if q not in QUERIES:
                print(f"Error: Unknown query '{q}'. Available: {', '.join(QUERIES.keys())}")
                sys.exit(1)
    else:
        selected_queries = list(QUERIES.keys())

    print(f"\n{'=' * 60}")
    print("Prefetch Benchmark")
    print(f"{'=' * 60}")
    print(f"Mode: {'cached' if args.cached else 'uncached'}")
    print(f"Queries: {', '.join(selected_queries)}")
    print(f"Runs per query: {args.runs}")
    print(f"{'=' * 60}\n")

    # Get git hashes
    master_hash = get_git_hash(MASTER_SOURCE_DIR)
    patch_hash = get_git_hash(PATCH_SOURCE_DIR)
    print(f"Master git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")

    # Verify/load data on each server (one at a time due to memory constraints)
    # Skip entirely if --skip-load is set
    if args.skip_load:
        master_version = None  # Will be fetched during benchmark run
        patch_version = None
    else:
        print("\n--- Verifying data on master ---")
        start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
        if not verify_data(MASTER_CONN, args.skip_load):
            print("Loading data on master...")
            load_data(MASTER_CONN)
        master_stats = extract_statistics(MASTER_CONN)
        master_version = get_pg_version(MASTER_CONN)
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(2)

        print("\n--- Verifying data on patch ---")
        start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
        if not verify_data(PATCH_CONN, args.skip_load):
            print("Loading data on patch...")
            load_data(PATCH_CONN)
        restore_statistics(PATCH_CONN, master_stats)
        patch_version = get_pg_version(PATCH_CONN)
        stop_server(patch_bin, PATCH_DATA_DIR)
        time.sleep(2)

    # Results storage
    results = {
        "timestamp": datetime.now().isoformat(),
        "master_hash": master_hash,
        "patch_hash": patch_hash,
        "master_version": master_version,
        "patch_version": patch_version,
        "mode": "cached" if args.cached else "uncached",
        "runs": args.runs,
        "queries": {},
    }

    # Initialize query results structure
    for query_id in selected_queries:
        query_def = QUERIES[query_id]
        results["queries"][query_id] = {
            "name": query_def["name"],
            "master": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_off": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_on": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
        }

    # Run all queries on master
    print(f"\n{'=' * 60}")
    print("Running all queries on MASTER")
    print(f"{'=' * 60}")
    master_start_time = time.time()
    start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
    try:
        master_conn = psycopg.connect(**MASTER_CONN)
        pin_backend(master_conn.info.backend_pid, args.benchmark_cpu)
        if master_version is None:
            master_version = get_pg_version(MASTER_CONN)
        with master_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in selected_queries:
            query_def = QUERIES[query_id]
            print(f"\n{query_id}: {query_def['name']} ({args.runs} runs)...")
            for run in range(args.runs):
                exec_time, explain_output = run_query(
                    master_conn, query_def, args.cached,
                    is_master=True, prefetch_setting=None,
                    benchmark_cpu=args.benchmark_cpu
                )
                if exec_time is not None:
                    results["queries"][query_id]["master"]["times"].append(exec_time)
                    # Save the last run's explain output
                    results["queries"][query_id]["master"]["explain"] = explain_output
                    print(f"  Run {run + 1}: {exec_time:.3f} ms")

        master_conn.close()
    finally:
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(2)
    master_end_time = time.time()

    # Run all queries on patch (both prefetch=off and prefetch=on)
    print(f"\n{'=' * 60}")
    print("Running all queries on PATCH")
    print(f"{'=' * 60}")
    patch_start_time = time.time()
    start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
    try:
        patch_conn = psycopg.connect(**PATCH_CONN)
        pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu)
        if patch_version is None:
            patch_version = get_pg_version(PATCH_CONN)
        with patch_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in selected_queries:
            query_def = QUERIES[query_id]

            # Run with prefetch OFF (skip if --prefetch-only)
            if not args.prefetch_only:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="off",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_off"]["times"].append(exec_time)
                        # Save the last run's explain output
                        results["queries"][query_id]["patch_off"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

            # Run with prefetch ON (skip if --prefetch-disabled)
            if not args.prefetch_disabled:
                print(f"\n{query_id}: {query_def['name']} (prefetch=on, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="on",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_on"]["times"].append(exec_time)
                        # Save the last run's explain output
                        results["queries"][query_id]["patch_on"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

        patch_conn.close()
    finally:
        stop_server(patch_bin, PATCH_DATA_DIR)
        time.sleep(2)
    patch_end_time = time.time()

    # Cleanup tmpfs if it was created
    if tmpfs_mount:
        cleanup_tmpfs(tmpfs_mount)

    # Calculate statistics and print summaries
    print(f"\n{'=' * 60}")
    print("RESULTS SUMMARY")
    print(f"{'=' * 60}")

    for query_id in selected_queries:
        query_results = results["queries"][query_id]

        # Calculate statistics
        for config in ["master", "patch_off", "patch_on"]:
            times = query_results[config]["times"]
            if times:
                query_results[config]["avg"] = mean(times)
                query_results[config]["min"] = min(times)
                query_results[config]["max"] = max(times)

        # Print summary for this query (using min as representative value)
        master_min = query_results["master"]["min"]
        patch_off_min = query_results["patch_off"]["min"]
        patch_on_min = query_results["patch_on"]["min"]

        # ANSI bold escape codes
        BOLD = "\033[1m"
        RESET = "\033[0m"

        print(f"\n{BOLD}{query_id}: {query_results['name']}{RESET}")
        if master_min:
            print(f"  master (min):               {master_min:10.3f} ms "
                  f"(avg={query_results['master']['avg']:.3f}, max={query_results['master']['max']:.3f})")
        if patch_off_min and master_min:
            ratio_off = patch_off_min / master_min
            print(f"  patch (prefetch=off) (min): {patch_off_min:10.3f} ms "
                  f"(avg={query_results['patch_off']['avg']:.3f}, max={query_results['patch_off']['max']:.3f}) "
                  f"[{BOLD}{ratio_off:.3f}x{RESET} vs master]")
        if patch_on_min and master_min:
            ratio_on = patch_on_min / master_min
            print(f"  patch (prefetch=on) (min):  {patch_on_min:10.3f} ms "
                  f"(avg={query_results['patch_on']['avg']:.3f}, max={query_results['patch_on']['max']:.3f}) "
                  f"[{BOLD}{ratio_on:.3f}x{RESET} vs master]")

        # Print query text and EXPLAIN ANALYZE outputs
        # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
        print()
        query_sql = QUERIES[query_id]["sql"].strip()
        print("  Query:")
        for line in query_sql.split('\n'):
            print(f"    {line.strip()}")
        print()
        if query_results["master"]["explain"]:
            print("  master EXPLAIN ANALYZE:")
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
    json_file = os.path.join(OUTPUT_DIR, f"benchmark_{timestamp}.json")
    txt_file = os.path.join(OUTPUT_DIR, f"benchmark_{timestamp}.txt")

    # Save JSON
    with open(json_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {json_file}")

    # Save human-readable text
    with open(txt_file, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("Prefetch Benchmark Results\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Timestamp: {results['timestamp']}\n")
        f.write(f"Master git hash: {results['master_hash']}\n")
        f.write(f"Patch git hash: {results['patch_hash']}\n")
        f.write(f"Mode: {results['mode']}\n")
        f.write(f"Runs per query: {results['runs']}\n\n")

        for query_id, qr in results["queries"].items():
            f.write(f"\n{query_id}: {qr['name']}\n")
            f.write("-" * 50 + "\n")

            master_min = qr["master"]["min"]
            patch_off_min = qr["patch_off"]["min"]
            patch_on_min = qr["patch_on"]["min"]

            if master_min:
                f.write(f"  master (min):               {master_min:10.3f} ms "
                        f"(avg={qr['master']['avg']:.3f}, max={qr['master']['max']:.3f})\n")
            if patch_off_min and master_min:
                ratio_off = patch_off_min / master_min
                f.write(f"  patch (prefetch=off) (min): {patch_off_min:10.3f} ms "
                        f"(avg={qr['patch_off']['avg']:.3f}, max={qr['patch_off']['max']:.3f}) "
                        f"[{ratio_off:.3f}x vs master]\n")
            if patch_on_min and master_min:
                ratio_on = patch_on_min / master_min
                f.write(f"  patch (prefetch=on) (min):  {patch_on_min:10.3f} ms "
                        f"(avg={qr['patch_on']['avg']:.3f}, max={qr['patch_on']['max']:.3f}) "
                        f"[{ratio_on:.3f}x vs master]\n")

            # Write query text and EXPLAIN ANALYZE outputs
            # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
            f.write("\n")
            query_sql = QUERIES[query_id]["sql"].strip()
            f.write("  Query:\n")
            for line in query_sql.split('\n'):
                f.write(f"    {line.strip()}\n")
            f.write("\n")
            if qr["master"]["explain"]:
                f.write("  master EXPLAIN ANALYZE:\n")
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

    def format_duration(seconds):
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        if mins == 0:
            return f"{secs} seconds"
        elif mins == 1:
            return f"1 minute {secs} seconds"
        else:
            return f"{mins} minutes {secs} seconds"

    print(f"\n{'=' * 60}")
    print("BENCHMARK RUN TIMES (excluding data loading)")
    print(f"{'=' * 60}")
    print(f"  Master:  {master_duration:10.1f} seconds ({format_duration(master_duration)})")
    print(f"  Patch:   {patch_duration:10.1f} seconds ({format_duration(patch_duration)})")
    print(f"  Total:   {total_duration:10.1f} seconds ({format_duration(total_duration)})")

    # Collect all patch runs with their ratios vs master (using min as representative)
    # Each patch configuration (prefetch=off, prefetch=on) is treated independently
    all_ratios = []
    for query_id in selected_queries:
        qr = results["queries"][query_id]
        master_min = qr["master"]["min"]
        if not master_min:
            continue

        # patch (prefetch=off)
        patch_off_min = qr["patch_off"]["min"]
        if patch_off_min:
            ratio = patch_off_min / master_min
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=off",
                "ratio": ratio,
                "master_ms": master_min,
                "patch_ms": patch_off_min,
            })

        # patch (prefetch=on)
        patch_on_min = qr["patch_on"]["min"]
        if patch_on_min:
            ratio = patch_on_min / master_min
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=on",
                "ratio": ratio,
                "master_ms": master_min,
                "patch_ms": patch_on_min,
            })

    # Improvements: ratio <= 1.00 (same speed or faster than master)
    # Regressions: ratio > 1.00 (slower than master)
    improvements = sorted([e for e in all_ratios if e["ratio"] <= 1.0], key=lambda x: x["ratio"])
    regressions = sorted([e for e in all_ratios if e["ratio"] > 1.0], key=lambda x: x["ratio"], reverse=True)

    BOLD = "\033[1m"
    RESET = "\033[0m"

    topn = args.topn
    print(f"\n{'=' * 60}")
    print(f"TOP {topn} IMPROVEMENTS vs MASTER (using min)")
    print(f"{'=' * 60}")
    if improvements:
        for entry in improvements[:topn]:
            print(f"  {entry['query_id']} ({entry['config']}): {entry['name']}")
            print(f"    {BOLD}{entry['ratio']:.3f}x{RESET} - master (min): {entry['master_ms']:.3f} ms, patch (min): {entry['patch_ms']:.3f} ms")
    else:
        print("  (none)")

    print(f"\n{'=' * 60}")
    print(f"TOP {topn} REGRESSIONS vs MASTER (using min)")
    print(f"{'=' * 60}")
    if regressions:
        for entry in regressions[:topn]:
            print(f"  {entry['query_id']} ({entry['config']}): {entry['name']}")
            print(f"    {BOLD}{entry['ratio']:.3f}x{RESET} - master (min): {entry['master_ms']:.3f} ms, patch (min): {entry['patch_ms']:.3f} ms")
    else:
        print("  (none)")


def run_readstream_tests(args):
    """Run readstream benchmark tests."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = MASTER_BIN
    patch_bin = PATCH_BIN

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(MASTER_BIN, tmpfs_mount, "master")
        patch_bin = copy_binaries_to_tmpfs(PATCH_BIN, tmpfs_mount, "patch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  master: {master_bin}")
        print(f"  patch: {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    queries = READSTREAM_QUERIES

    print(f"\n{'=' * 60}")
    print("Readstream Benchmark Tests")
    print(f"{'=' * 60}")
    print(f"Queries: {', '.join(queries.keys())}")
    print(f"Runs per query: {args.runs}")
    print(f"{'=' * 60}\n")

    # Get git hashes
    master_hash = get_git_hash(MASTER_SOURCE_DIR)
    patch_hash = get_git_hash(PATCH_SOURCE_DIR)
    print(f"Master git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")

    # Verify/load data on each server
    print("\n--- Verifying data on master ---")
    start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
    if not verify_readstream_data(MASTER_CONN):
        print("Loading data on master...")
        load_readstream_data(MASTER_CONN)
    master_version = get_pg_version(MASTER_CONN)
    stop_server(master_bin, MASTER_DATA_DIR)
    time.sleep(2)

    print("\n--- Verifying data on patch ---")
    start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
    if not verify_readstream_data(PATCH_CONN):
        print("Loading data on patch...")
        load_readstream_data(PATCH_CONN)
    patch_version = get_pg_version(PATCH_CONN)
    stop_server(patch_bin, PATCH_DATA_DIR)
    time.sleep(2)

    # Results storage
    results = {
        "timestamp": datetime.now().isoformat(),
        "master_hash": master_hash,
        "patch_hash": patch_hash,
        "master_version": master_version,
        "patch_version": patch_version,
        "mode": "readstream",
        "runs": args.runs,
        "queries": {},
    }

    # Initialize query results structure
    for query_id in queries.keys():
        query_def = queries[query_id]
        results["queries"][query_id] = {
            "name": query_def["name"],
            "master": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_off": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_on": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
        }

    # Run all queries on master
    print(f"\n{'=' * 60}")
    print("Running all queries on MASTER")
    print(f"{'=' * 60}")
    start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
    try:
        master_conn = psycopg.connect(**MASTER_CONN)
        pin_backend(master_conn.info.backend_pid, args.benchmark_cpu)
        with master_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in queries.keys():
            query_def = queries[query_id]
            print(f"\n{query_id}: {query_def['name']} ({args.runs} runs)...")
            for run in range(args.runs):
                exec_time, explain_output = run_query(
                    master_conn, query_def, args.cached,
                    is_master=True, prefetch_setting=None,
                    benchmark_cpu=args.benchmark_cpu
                )
                if exec_time is not None:
                    results["queries"][query_id]["master"]["times"].append(exec_time)
                    results["queries"][query_id]["master"]["explain"] = explain_output
                    print(f"  Run {run + 1}: {exec_time:.3f} ms")

        master_conn.close()
    finally:
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(2)

    # Run all queries on patch (both prefetch=off and prefetch=on)
    print(f"\n{'=' * 60}")
    print("Running all queries on PATCH")
    print(f"{'=' * 60}")
    start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
    try:
        patch_conn = psycopg.connect(**PATCH_CONN)
        pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu)
        with patch_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in queries.keys():
            query_def = queries[query_id]

            # Run with prefetch OFF
            if not args.prefetch_only:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="off",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_off"]["times"].append(exec_time)
                        results["queries"][query_id]["patch_off"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

            # Run with prefetch ON
            if not args.prefetch_disabled:
                print(f"\n{query_id}: {query_def['name']} (prefetch=on, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="on",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_on"]["times"].append(exec_time)
                        results["queries"][query_id]["patch_on"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

        patch_conn.close()
    finally:
        stop_server(patch_bin, PATCH_DATA_DIR)
        time.sleep(2)

    # Cleanup tmpfs if it was created
    if tmpfs_mount:
        cleanup_tmpfs(tmpfs_mount)

    # Calculate statistics and print summaries
    print(f"\n{'=' * 60}")
    print("RESULTS SUMMARY")
    print(f"{'=' * 60}")

    BOLD = "\033[1m"
    RESET = "\033[0m"

    for query_id in queries.keys():
        query_results = results["queries"][query_id]

        # Calculate statistics
        for config in ["master", "patch_off", "patch_on"]:
            times = query_results[config]["times"]
            if times:
                query_results[config]["avg"] = mean(times)
                query_results[config]["min"] = min(times)
                query_results[config]["max"] = max(times)

        # Use min as representative value
        master_min = query_results["master"]["min"]
        patch_off_min = query_results["patch_off"]["min"]
        patch_on_min = query_results["patch_on"]["min"]

        print(f"\n{BOLD}{query_id}: {query_results['name']}{RESET}")
        if master_min:
            print(f"  master (min):               {master_min:10.3f} ms "
                  f"(avg={query_results['master']['avg']:.3f}, max={query_results['master']['max']:.3f})")
        if patch_off_min and master_min:
            ratio_off = patch_off_min / master_min
            print(f"  patch (prefetch=off) (min): {patch_off_min:10.3f} ms "
                  f"(avg={query_results['patch_off']['avg']:.3f}, max={query_results['patch_off']['max']:.3f}) "
                  f"[{BOLD}{ratio_off:.3f}x{RESET} vs master]")
        if patch_on_min and master_min:
            ratio_on = patch_on_min / master_min
            print(f"  patch (prefetch=on) (min):  {patch_on_min:10.3f} ms "
                  f"(avg={query_results['patch_on']['avg']:.3f}, max={query_results['patch_on']['max']:.3f}) "
                  f"[{BOLD}{ratio_on:.3f}x{RESET} vs master]")

        # Print query text and EXPLAIN ANALYZE outputs
        print()
        query_sql = queries[query_id]["sql"].strip()
        print("  Query:")
        for line in query_sql.split('\n'):
            print(f"    {line.strip()}")
        print()
        if query_results["master"]["explain"]:
            print("  master EXPLAIN ANALYZE:")
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

    print(f"\n{'=' * 60}")
    print("Done.")


def run_random_backwards_tests(args):
    """Run random backwards benchmark tests."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = MASTER_BIN
    patch_bin = PATCH_BIN

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(MASTER_BIN, tmpfs_mount, "master")
        patch_bin = copy_binaries_to_tmpfs(PATCH_BIN, tmpfs_mount, "patch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  master: {master_bin}")
        print(f"  patch: {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    queries = RANDOM_BACKWARDS_QUERIES

    print(f"\n{'=' * 60}")
    print("Random Backwards Benchmark Tests")
    print(f"{'=' * 60}")
    print(f"Queries: {', '.join(queries.keys())}")
    print(f"Runs per query: {args.runs}")
    print(f"{'=' * 60}\n")

    # Get git hashes
    master_hash = get_git_hash(MASTER_SOURCE_DIR)
    patch_hash = get_git_hash(PATCH_SOURCE_DIR)
    print(f"Master git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")

    # Verify/load data on each server
    print("\n--- Verifying data on master ---")
    start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
    if not verify_random_backwards_data(MASTER_CONN):
        print("Loading data on master...")
        load_random_backwards_data(MASTER_CONN)
    master_version = get_pg_version(MASTER_CONN)
    stop_server(master_bin, MASTER_DATA_DIR)
    time.sleep(2)

    print("\n--- Verifying data on patch ---")
    start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
    if not verify_random_backwards_data(PATCH_CONN):
        print("Loading data on patch...")
        load_random_backwards_data(PATCH_CONN)
    patch_version = get_pg_version(PATCH_CONN)
    stop_server(patch_bin, PATCH_DATA_DIR)
    time.sleep(2)

    # Results storage
    results = {
        "timestamp": datetime.now().isoformat(),
        "master_hash": master_hash,
        "patch_hash": patch_hash,
        "master_version": master_version,
        "patch_version": patch_version,
        "mode": "random_backwards",
        "runs": args.runs,
        "queries": {},
    }

    # Initialize query results structure
    for query_id in queries.keys():
        query_def = queries[query_id]
        results["queries"][query_id] = {
            "name": query_def["name"],
            "master": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_off": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
            "patch_on": {"times": [], "avg": None, "min": None, "max": None, "explain": None},
        }

    # Run all queries on master
    print(f"\n{'=' * 60}")
    print("Running all queries on MASTER")
    print(f"{'=' * 60}")
    start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
    try:
        master_conn = psycopg.connect(**MASTER_CONN)
        pin_backend(master_conn.info.backend_pid, args.benchmark_cpu)
        with master_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in queries.keys():
            query_def = queries[query_id]
            print(f"\n{query_id}: {query_def['name']} ({args.runs} runs)...")
            for run in range(args.runs):
                exec_time, explain_output = run_query(
                    master_conn, query_def, args.cached,
                    is_master=True, prefetch_setting=None,
                    benchmark_cpu=args.benchmark_cpu
                )
                if exec_time is not None:
                    results["queries"][query_id]["master"]["times"].append(exec_time)
                    results["queries"][query_id]["master"]["explain"] = explain_output
                    print(f"  Run {run + 1}: {exec_time:.3f} ms")

        master_conn.close()
    finally:
        stop_server(master_bin, MASTER_DATA_DIR)
        time.sleep(2)

    # Run all queries on patch (both prefetch=off and prefetch=on)
    print(f"\n{'=' * 60}")
    print("Running all queries on PATCH")
    print(f"{'=' * 60}")
    start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
    try:
        patch_conn = psycopg.connect(**PATCH_CONN)
        pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu)
        with patch_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        for query_id in queries.keys():
            query_def = queries[query_id]

            # Run with prefetch OFF
            if not args.prefetch_only:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="off",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_off"]["times"].append(exec_time)
                        results["queries"][query_id]["patch_off"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

            # Run with prefetch ON
            if not args.prefetch_disabled:
                print(f"\n{query_id}: {query_def['name']} (prefetch=on, {args.runs} runs)...")
                for run in range(args.runs):
                    exec_time, explain_output = run_query(
                        patch_conn, query_def, args.cached,
                        is_master=False, prefetch_setting="on",
                        benchmark_cpu=args.benchmark_cpu
                    )
                    if exec_time is not None:
                        results["queries"][query_id]["patch_on"]["times"].append(exec_time)
                        results["queries"][query_id]["patch_on"]["explain"] = explain_output
                        print(f"  Run {run + 1}: {exec_time:.3f} ms")

        patch_conn.close()
    finally:
        stop_server(patch_bin, PATCH_DATA_DIR)
        time.sleep(2)

    # Cleanup tmpfs if it was created
    if tmpfs_mount:
        cleanup_tmpfs(tmpfs_mount)

    # Calculate statistics and print summaries
    print(f"\n{'=' * 60}")
    print("RESULTS SUMMARY")
    print(f"{'=' * 60}")

    BOLD = "\033[1m"
    RESET = "\033[0m"

    for query_id in queries.keys():
        query_results = results["queries"][query_id]

        # Calculate statistics
        for config in ["master", "patch_off", "patch_on"]:
            times = query_results[config]["times"]
            if times:
                query_results[config]["avg"] = mean(times)
                query_results[config]["min"] = min(times)
                query_results[config]["max"] = max(times)

        # Use min as representative value
        master_min = query_results["master"]["min"]
        patch_off_min = query_results["patch_off"]["min"]
        patch_on_min = query_results["patch_on"]["min"]

        print(f"\n{BOLD}{query_id}: {query_results['name']}{RESET}")
        if master_min:
            print(f"  master (min):               {master_min:10.3f} ms "
                  f"(avg={query_results['master']['avg']:.3f}, max={query_results['master']['max']:.3f})")
        if patch_off_min and master_min:
            ratio_off = patch_off_min / master_min
            print(f"  patch (prefetch=off) (min): {patch_off_min:10.3f} ms "
                  f"(avg={query_results['patch_off']['avg']:.3f}, max={query_results['patch_off']['max']:.3f}) "
                  f"[{BOLD}{ratio_off:.3f}x{RESET} vs master]")
        if patch_on_min and master_min:
            ratio_on = patch_on_min / master_min
            print(f"  patch (prefetch=on) (min):  {patch_on_min:10.3f} ms "
                  f"(avg={query_results['patch_on']['avg']:.3f}, max={query_results['patch_on']['max']:.3f}) "
                  f"[{BOLD}{ratio_on:.3f}x{RESET} vs master]")

        # Print query text and EXPLAIN ANALYZE outputs
        print()
        query_sql = queries[query_id]["sql"].strip()
        print("  Query:")
        for line in query_sql.split('\n'):
            print(f"    {line.strip()}")
        print()
        if query_results["master"]["explain"]:
            print("  master EXPLAIN ANALYZE:")
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

    print(f"\n{'=' * 60}")
    print("Done.")


def run_stress_test(args):
    """Run stress test mode: randomly generate queries to find regressions."""
    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = MASTER_BIN
    patch_bin = PATCH_BIN

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        master_bin = copy_binaries_to_tmpfs(MASTER_BIN, tmpfs_mount, "master")
        patch_bin = copy_binaries_to_tmpfs(PATCH_BIN, tmpfs_mount, "patch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  master: {master_bin}")
        print(f"  patch: {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    print("=" * 60)
    print("STRESS TEST MODE")
    print("=" * 60)
    print(f"Looking for regressions >= {STRESS_REGRESSION_THRESHOLD:.0%} slower than master")
    print(f"Generating {STRESS_QUERIES_PER_BATCH} queries per batch")
    print(f"Minimum query duration: {args.min_query_ms:.1f} ms")
    print(f"Mode: {'cached' if args.cached else 'uncached'}")
    print("=" * 60)

    # Get git hashes
    master_hash = get_git_hash(MASTER_SOURCE_DIR)
    patch_hash = get_git_hash(PATCH_SOURCE_DIR)
    print(f"\nMaster git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")

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

            # Run all queries on master, replacing any that are too fast
            print(f"\n--- Running on MASTER ---")
            start_server(master_bin, "master", MASTER_DATA_DIR, MASTER_CONN)
            try:
                master_conn = psycopg.connect(**MASTER_CONN)
                pin_backend(master_conn.info.backend_pid, args.benchmark_cpu)
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
                            exec_time, explain_output = run_query(
                                master_conn, query_def, args.cached,
                                is_master=True, prefetch_setting=None,
                                benchmark_cpu=args.benchmark_cpu
                            )
                            if exec_time is not None:
                                if exec_time < args.min_query_ms:
                                    replacement_count += 1
                                    print(f"{exec_time:.3f} ms (too fast, regenerating...)")
                                    # Generate a replacement query with same ID
                                    query_num = int(query_id[1:])  # Extract number from "S123"
                                    query_def = generate_random_query(query_num + replacement_count * 1000)
                                    queries[i] = (query_id, query_def)
                                    # Update results dict for new query
                                    results[query_id] = {
                                        "query_def": query_def,
                                        "master": {"times": [], "avg": None, "explain": None},
                                        "patch_off": {"times": [], "avg": None, "explain": None},
                                        "patch_on": {"times": [], "avg": None, "explain": None},
                                    }
                                    continue  # Try again with new query
                                else:
                                    results[query_id]["master"]["times"].append(exec_time)
                                    results[query_id]["master"]["min"] = exec_time
                                    results[query_id]["master"]["explain"] = explain_output
                                    print(f"{exec_time:.3f} ms")
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
                stop_server(master_bin, MASTER_DATA_DIR)
                time.sleep(2)

            # Run all queries on patch
            print(f"\n--- Running on PATCH ---")
            start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
            try:
                patch_conn = psycopg.connect(**PATCH_CONN)
                pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu)
                with patch_conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                    # Stress test GUC settings
                    cur.execute("SET enable_bitmapscan = off")
                    cur.execute("SET random_page_cost = 1.1")
                    cur.execute("SET max_parallel_workers_per_gather = 0")

                BOLD = "\033[1m"
                RESET = "\033[0m"

                for query_id, query_def in queries:
                    master_min = results[query_id]["master"]["min"]

                    print(f"\n  {query_id}: {query_def['name']}")
                    # Print query text
                    for line in query_def['sql'].strip().split('\n'):
                        print(f"    {line.strip()}")

                    # Test with prefetch OFF (skip if --prefetch-only)
                    if not args.prefetch_only:
                        print(f"  prefetch=off...", end=" ", flush=True)
                        try:
                            exec_time, explain_output = run_query(
                                patch_conn, query_def, args.cached,
                                is_master=False, prefetch_setting="off",
                                benchmark_cpu=args.benchmark_cpu
                            )
                            if exec_time is not None:
                                results[query_id]["patch_off"]["times"].append(exec_time)
                                results[query_id]["patch_off"]["min"] = exec_time
                                results[query_id]["patch_off"]["explain"] = explain_output
                                if master_min:
                                    ratio = exec_time / master_min
                                    print(f"{exec_time:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs master)")
                                else:
                                    print(f"{exec_time:.3f} ms")
                            else:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                        except Exception as e:
                            print(f"ERROR: {e}")
                            print("\nGenerated invalid SQL! This is a bug in the query generator.")
                            print(f"Query: {query_def['sql']}")
                            sys.exit(1)

                    # Test with prefetch ON (skip if --prefetch-disabled)
                    if not args.prefetch_disabled:
                        print(f"  prefetch=on...", end=" ", flush=True)
                        try:
                            exec_time, explain_output = run_query(
                                patch_conn, query_def, args.cached,
                                is_master=False, prefetch_setting="on",
                                benchmark_cpu=args.benchmark_cpu
                            )
                            if exec_time is not None:
                                results[query_id]["patch_on"]["times"].append(exec_time)
                                results[query_id]["patch_on"]["min"] = exec_time
                                results[query_id]["patch_on"]["explain"] = explain_output
                                if master_min:
                                    ratio = exec_time / master_min
                                    print(f"{exec_time:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs master)")
                                else:
                                    print(f"{exec_time:.3f} ms")
                            else:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                        except Exception as e:
                            print(f"ERROR: {e}")
                            print("\nGenerated invalid SQL! This is a bug in the query generator.")
                            print(f"Query: {query_def['sql']}")
                            sys.exit(1)

                patch_conn.close()
            finally:
                stop_server(patch_bin, PATCH_DATA_DIR)
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

                start_server(patch_bin, "patch", PATCH_DATA_DIR, PATCH_CONN)
                try:
                    patch_conn = psycopg.connect(**PATCH_CONN)
                    pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu)
                    with patch_conn.cursor() as cur:
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
                        cur.execute("SET enable_bitmapscan = off")
                        cur.execute("SET random_page_cost = 1.1")
                        cur.execute("SET max_parallel_workers_per_gather = 0")

                    for reg in regressions_found:
                        query_def = reg["query_def"]
                        master_avg = reg["master_ms"]
                        prefetch_setting = "off" if reg["config"] == "prefetch=off" else "on"

                        print(f"\n  Verifying {reg['query_id']} ({reg['config']}, initial {reg['ratio']:.3f}x)...")

                        # Exponential backoff: 2 * 1.4^n seconds, 10 retries, ~140 seconds total
                        RETRY_COUNT = 10
                        RETRY_BASE = 2.0
                        RETRY_MULTIPLIER = 1.4

                        regression_confirmed = True
                        for retry in range(RETRY_COUNT):
                            delay = RETRY_BASE * (RETRY_MULTIPLIER ** retry)
                            print(f"    Retry {retry + 1}/{RETRY_COUNT}: waiting {delay:.1f}s...", end=" ", flush=True)
                            time.sleep(delay)

                            exec_time, _ = run_query(
                                patch_conn, query_def, args.cached,
                                is_master=False, prefetch_setting=prefetch_setting,
                                benchmark_cpu=args.benchmark_cpu
                            )

                            if exec_time is not None:
                                ratio = exec_time / master_avg
                                print(f"{exec_time:.3f} ms ({ratio:.3f}x)")

                                if ratio < STRESS_REGRESSION_THRESHOLD:
                                    print(f"    Under threshold - spurious failure, discarding")
                                    regression_confirmed = False
                                    break
                            else:
                                print("FAILED")

                        if regression_confirmed:
                            print(f"    All retries confirm regression")
                            confirmed_regressions.append(reg)

                    patch_conn.close()
                finally:
                    stop_server(patch_bin, PATCH_DATA_DIR)
                    time.sleep(2)

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
                print(f"Patch ({worst['config']}) is {worst['ratio']:.3f}x slower than master ({pct:.1f}% regression)")
                print()
                print(f"Master:           {worst['master_ms']:.3f} ms")

                query_def = worst["query_def"]
                r = results[worst["query_id"]]

                if r["patch_off"]["min"]:
                    ratio_off = r["patch_off"]["min"] / worst["master_ms"]
                    marker = " <-- REGRESSION" if worst["config"] == "prefetch=off" else ""
                    print(f"Patch (off):      {r['patch_off']['min']:.3f} ms ({ratio_off:.3f}x vs master){marker}")

                if r["patch_on"]["min"]:
                    ratio_on = r["patch_on"]["min"] / worst["master_ms"]
                    marker = " <-- REGRESSION" if worst["config"] == "prefetch=on" else ""
                    print(f"Patch (on):       {r['patch_on']['min']:.3f} ms ({ratio_on:.3f}x vs master){marker}")

                # Print EXPLAIN ANALYZE output
                print()
                print("EXPLAIN ANALYZE output:")
                print()
                if r["master"]["explain"]:
                    print("  master:")
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

                # Print query definition ready to add to QUERIES
                print()
                print("Add this to QUERIES dict:")
                print()

                # Format evict list
                evict_str = ", ".join(f'"{t}"' for t in query_def["evict"])
                prewarm_idx_str = ", ".join(f'"{i}"' for i in query_def["prewarm_indexes"])
                prewarm_tbl_str = ", ".join(f'"{t}"' for t in query_def["prewarm_tables"])

                # Clean up SQL formatting
                sql_lines = query_def["sql"].strip().split('\n')
                sql_formatted = '\n'.join('            ' + line.strip() for line in sql_lines)

                print(f'    ("STRESS_{total_queries_tested}", {{')
                print(f'        "name": "{query_def["name"]}",')
                print(f'        "sql": """')
                print(sql_formatted)
                print(f'        """,')
                print(f'        "evict": [{evict_str}],')
                print(f'        "prewarm_indexes": [{prewarm_idx_str}],')
                print(f'        "prewarm_tables": [{prewarm_tbl_str}],')
                print(f'    }}),')
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


def main():
    args = parse_arguments()
    if args.stress_test:
        run_stress_test(args)
    elif args.readstream_tests:
        run_readstream_tests(args)
    elif args.random_backwards_tests:
        run_random_backwards_tests(args)
    else:
        run_benchmark(args)


if __name__ == "__main__":
    main()
