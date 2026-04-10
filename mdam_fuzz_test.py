#!/usr/bin/env python3
"""
MDAM OR-clause optimizer fuzz tester.

Generates random WHERE clauses targeting multi-column btree indexes,
executes each query two ways (MDAM index path vs sequential scan + sort),
and compares results row-by-row for both content and ordering correctness.

Usage:
    python3 mdam_fuzz_test.py [--seed N] [--duration SECONDS] [--workers N]
                              [--setup-only] [--verbose]
"""

import argparse
import multiprocessing
import os
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg

# ---------------------------------------------------------------------------
# Configurable probability / structure knobs
# ---------------------------------------------------------------------------

PRED_CONFIG: Dict[str, Any] = {
    # Top-level OR structure
    "or_branches_min": 2,
    "or_branches_max": 6,

    # Predicates per AND-conjunct.  conjunct_size_weights biases how many
    # of the index's columns each conjunct constrains, drawn before leaf
    # generation; columns outside the chosen subset are omitted.  Lower
    # numbers => looser arms => more rows actually match.
    "conjunct_size_weights": {1: 1, 2: 5, 3: 3, 4: 1},

    # Nesting limits.  Raised max_leaf_predicates so the top-level OR
    # doesn't collapse to a single AND-conjunct when the first branch's
    # leaves alone exceed the cap (which left MDAM with nothing to chew on).
    "max_depth": 3,
    "max_leaf_predicates": 50,
    "max_or_branches_total": 12,

    # Leaf predicate type weights (relative, normalised internally).
    # These apply when generating a predicate for a column in the *normal*
    # path (no skip-scan bias).  Cross-type and ROW_CMP atoms are
    # unconditional MDAM bail-outs, so their weights stay zeroed -- we
    # want to exercise the transformation, not the bail gates.
    "w_equality": 30,
    "w_range": 20,          # <, <=, >, >=
    "w_in_list": 20,
    "w_is_null": 10,
    "w_is_not_null": 10,
    "w_row_compare": 0,
    "w_cross_type": 0,
    "w_omit": 15,           # column unconstrained

    # Skip-scan emphasis: when a higher-order index column already has a
    # condition, lower-order columns use these *extra* weights for omit /
    # inequality-only.  They are added to w_omit / w_range respectively.
    "w_skip_scan_omit": 20,
    "w_skip_scan_inequality": 15,

    # Probability of replacing a leaf with a nested OR sub-tree.  Nested
    # ORs multiply DNF conjunct count: a single AND (B OR C) doubles the
    # arms in that branch, and depth-3 trees can produce hundreds of DNF
    # arms.  The old comment warned this blew up MDAM_MAX_DNF_CONJUNCTS;
    # with EXPLAIN-retry filtering rejected candidates, nesting can be
    # turned on to exercise MDAM's DNF expansion and shattering paths.
    "nested_or_probability": 0.25,

    # Special predicate injection.  Contradictory atoms cause the planner
    # to short-circuit with "One-Time Filter: false" before MDAM runs, so
    # they don't exercise the transform either.  Redundant atoms are also
    # zeroed because the random second leaf often collides with the first
    # in an unsatisfiable way (e.g. a=63 AND a IN (20)), which MDAM detects
    # and drops -- shrinking the surviving DNF below the 2-arm minimum.
    "redundant_probability": 0.0,
    "contradictory_probability": 0.0,

    # Scan direction
    "desc_probability": 0.30,

    # IN-list size bounds.  Larger IN-lists raise the per-arm match
    # population (each extra value is another row partition that survives
    # the conjunct) and add more critical points for shattering.  The old
    # 2..4 cap was meant to keep the cartesian product under
    # MDAM_MAX_RETRIEVALS; with EXPLAIN-retry filtering rejected
    # candidates that constraint is now redundant, so the cap can ride
    # higher.
    "in_list_min": 3,
    "in_list_max": 16,

    # Statement timeout (ms)
    "statement_timeout_ms": 15000,

    # Per-test cap on EXPLAIN retries when MDAM doesn't fire on the
    # generated predicate.  We regenerate and re-EXPLAIN until MDAM is
    # picked, or this cap is hit; if all candidates fail we run the last
    # one anyway so the skip remains visible in the summary.
    "max_explain_retries": 20,
}

# ---------------------------------------------------------------------------
# Database connection parameters
# ---------------------------------------------------------------------------

DB_PARAMS = {"host": "localhost", "dbname": "regression", "user": "pg"}

# ---------------------------------------------------------------------------
# Table / index definitions
# ---------------------------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    sql_type: str           # "int", "text"
    lo: Any = None          # inclusive lower bound (int) or None
    hi: Any = None          # inclusive upper bound (int) or None
    text_values: Optional[List[str]] = None  # for text columns


@dataclass
class IndexDef:
    name: str
    columns: List[str]


@dataclass
class TableDef:
    name: str
    columns: List[ColumnDef]
    indexes: List[IndexDef]
    row_count: int
    insert_sql: str
    weight: float
    # Seed for setseed() before INSERT.  Distinct seeds per table keep the
    # tables' streams independent; identical seeds across reloads make
    # the generated data byte-for-byte reproducible.
    seed: float = 0.0

    def col_by_name(self, name: str) -> ColumnDef:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)


TEXT_VALUES = [f"val_{i:03d}" for i in range(50)]

# Cardinalities increase with each lower-order index column, so the
# leading column always has the fewest distinct values.  That's the shape
# MDAM is designed to exploit: a small set of leading-column points means
# shattering produces few retrievals, and the long tail of distinct
# values on trailing columns gives skip-scan something to skip over.
#
# Data is generated by random() seeded via setseed(table.seed) before
# each INSERT.  Each column draws independently from the PRNG stream, so
# the joint distribution is uniform across all pairs (every (a, b) pair
# appears roughly row_count/(card_a * card_b) times).  The seed makes the
# data byte-for-byte reproducible across reloads.
TABLES: List[TableDef] = [
    TableDef(
        name="fuzz_int4",
        columns=[
            ColumnDef("a", "int", 1, 9),
            ColumnDef("b", "int", 1, 49),
            ColumnDef("c", "int", 1, 199),
            ColumnDef("d", "int", 1, 999),
        ],
        indexes=[IndexDef("fuzz_int4_abcd", ["a", "b", "c", "d"])],
        row_count=200_000,
        seed=0.4242,
        insert_sql="""\
INSERT INTO fuzz_int4
SELECT NULLIF(floor(random() * 10)::int, 0),
       NULLIF(floor(random() * 50)::int, 0),
       NULLIF(floor(random() * 200)::int, 0),
       NULLIF(floor(random() * 1000)::int, 0)
FROM generate_series(1, 200000)""",
        weight=0.70,
    ),
    TableDef(
        name="fuzz_mixed",
        columns=[
            ColumnDef("x", "int", 1, 9),
            ColumnDef("y", "text", text_values=TEXT_VALUES),
            ColumnDef("z", "int", 1, 199),
            ColumnDef("w", "int", 1, 499),
        ],
        indexes=[IndexDef("fuzz_mixed_xyzw", ["x", "y", "z", "w"])],
        row_count=50_000,
        seed=0.1717,
        insert_sql="""\
INSERT INTO fuzz_mixed
SELECT NULLIF(floor(random() * 10)::int, 0),
       CASE WHEN random() < 0.01 THEN NULL
            ELSE 'val_' || lpad(floor(random() * 50)::text, 3, '0') END,
       NULLIF(floor(random() * 200)::int, 0),
       NULLIF(floor(random() * 500)::int, 0)
FROM generate_series(1, 50000)""",
        weight=0.15,
    ),
    TableDef(
        name="fuzz_multi_idx",
        columns=[
            ColumnDef("p", "int", 1, 9),
            ColumnDef("q", "int", 1, 19),
            ColumnDef("r", "int", 1, 99),
            ColumnDef("s", "int", 1, 199),
        ],
        indexes=[
            IndexDef("fuzz_mi_pqrs", ["p", "q", "r", "s"]),
            IndexDef("fuzz_mi_qpsr", ["q", "p", "s", "r"]),
        ],
        row_count=100_000,
        seed=0.8585,
        insert_sql="""\
INSERT INTO fuzz_multi_idx
SELECT NULLIF(floor(random() * 10)::int, 0),
       NULLIF(floor(random() * 20)::int, 0),
       NULLIF(floor(random() * 100)::int, 0),
       NULLIF(floor(random() * 200)::int, 0)
FROM generate_series(1, 100000)""",
        weight=0.15,
    ),
]

INDEX_COLUMNS_BY_NAME: Dict[str, List[str]] = {
    idx.name: list(idx.columns) for tab in TABLES for idx in tab.indexes
}

# ---------------------------------------------------------------------------
# Predicate tree AST
# ---------------------------------------------------------------------------


class PredKind(Enum):
    AND = auto()
    OR = auto()
    LEAF = auto()


@dataclass
class PredNode:
    kind: PredKind
    children: List["PredNode"] = field(default_factory=list)
    # LEAF fields:
    column: Optional[str] = None
    op: Optional[str] = None
    value: Any = None
    cross_type: bool = False
    # For ROW_CMP: columns and values are lists
    row_columns: Optional[List[str]] = None
    row_values: Optional[List[Any]] = None


# ---------------------------------------------------------------------------
# Value generation helpers
# ---------------------------------------------------------------------------


def random_int_value(rng: random.Random, col: ColumnDef) -> int:
    """Pick a random value from the column's domain."""
    return rng.randint(col.lo, col.hi)


def random_text_value(rng: random.Random, col: ColumnDef) -> str:
    return rng.choice(col.text_values)


def random_value(rng: random.Random, col: ColumnDef) -> Any:
    if col.sql_type == "text":
        return random_text_value(rng, col)
    return random_int_value(rng, col)


def sql_literal(val: Any, col: ColumnDef, cross_type: bool = False) -> str:
    """Render a Python value as a SQL literal."""
    if val is None:
        return "NULL"
    if col.sql_type == "text":
        escaped = str(val).replace("'", "''")
        return f"'{escaped}'"
    s = str(val)
    if cross_type:
        s += "::bigint"
    return s


# ---------------------------------------------------------------------------
# Predicate generation
# ---------------------------------------------------------------------------


def _weighted_choice(rng: random.Random, items_weights: List[Tuple[str, float]]) -> str:
    """Weighted random choice from (item, weight) pairs."""
    total = sum(w for _, w in items_weights)
    r = rng.random() * total
    cumulative = 0.0
    for item, w in items_weights:
        cumulative += w
        if r <= cumulative:
            return item
    return items_weights[-1][0]


def generate_leaf(
    rng: random.Random,
    col: ColumnDef,
    table: TableDef,
    index: IndexDef,
    skip_scan_bias: bool,
    allow_omit: bool = True,
) -> Optional[PredNode]:
    """Generate a single leaf predicate for a column, or None (omit)."""
    cfg = PRED_CONFIG
    is_text = col.sql_type == "text"

    # Build weight table
    weights: List[Tuple[str, float]] = []
    weights.append(("equality", cfg["w_equality"]))
    weights.append(("range", cfg["w_range"] + (cfg["w_skip_scan_inequality"] if skip_scan_bias else 0)))
    weights.append(("in_list", cfg["w_in_list"]))
    weights.append(("is_null", cfg["w_is_null"]))
    weights.append(("is_not_null", cfg["w_is_not_null"]))
    omit_w = 0 if not allow_omit else \
        cfg["w_omit"] + (cfg["w_skip_scan_omit"] if skip_scan_bias else 0)
    weights.append(("omit", omit_w))

    if not is_text:
        weights.append(("row_compare", cfg["w_row_compare"]))
        weights.append(("cross_type", cfg["w_cross_type"]))

    choice = _weighted_choice(rng, weights)

    if choice == "omit":
        return None

    if choice == "equality":
        v = random_value(rng, col)
        return PredNode(kind=PredKind.LEAF, column=col.name, op="=", value=v)

    if choice == "range":
        op = rng.choice(["<", "<=", ">", ">="])
        v = random_value(rng, col)
        return PredNode(kind=PredKind.LEAF, column=col.name, op=op, value=v)

    if choice == "in_list":
        n = rng.randint(cfg["in_list_min"], cfg["in_list_max"])
        vals = sorted(set(random_value(rng, col) for _ in range(n)))
        if not vals:
            vals = [random_value(rng, col)]
        return PredNode(kind=PredKind.LEAF, column=col.name, op="IN", value=vals)

    if choice == "is_null":
        return PredNode(kind=PredKind.LEAF, column=col.name, op="IS NULL")

    if choice == "is_not_null":
        return PredNode(kind=PredKind.LEAF, column=col.name, op="IS NOT NULL")

    if choice == "row_compare":
        col_idx = index.columns.index(col.name)
        remaining = len(index.columns) - col_idx
        if remaining < 2:
            # Fall back to simple range
            op = rng.choice(["<", "<=", ">", ">="])
            v = random_value(rng, col)
            return PredNode(kind=PredKind.LEAF, column=col.name, op=op, value=v)
        n_cols = rng.randint(2, min(3, remaining))
        cols = index.columns[col_idx : col_idx + n_cols]
        vals = [random_value(rng, table.col_by_name(c)) for c in cols]
        op = rng.choice(["<", "<=", ">", ">="])
        return PredNode(
            kind=PredKind.LEAF,
            column=col.name,
            op="ROW_CMP",
            row_columns=cols,
            row_values=vals,
            value=op,
        )

    if choice == "cross_type":
        v = random_int_value(rng, col)
        return PredNode(kind=PredKind.LEAF, column=col.name, op="=", value=v, cross_type=True)

    return None


def generate_conjunct(
    rng: random.Random,
    table: TableDef,
    index: IndexDef,
    depth: int,
    leaf_count: List[int],
) -> PredNode:
    """Generate an AND-conjunct of leaf predicates over index columns."""
    cfg = PRED_CONFIG
    children: List[PredNode] = []
    has_higher_condition = False

    # Bias toward fewer predicates per conjunct: pick a target size, then
    # constrain only that many of the index's columns (chosen at random,
    # preserving index order).  Looser arms are far more likely to match
    # rows, raising the useful-coverage rate.
    size_weights = cfg["conjunct_size_weights"]
    n_cols = len(index.columns)
    target = _weighted_choice(
        rng,
        [(sz, w) for sz, w in size_weights.items() if sz <= n_cols],
    )
    chosen_positions = set(rng.sample(range(n_cols), target))

    for pos, col_name in enumerate(index.columns):
        if leaf_count[0] >= cfg["max_leaf_predicates"]:
            break
        if pos not in chosen_positions:
            continue

        col = table.col_by_name(col_name)
        skip_scan_bias = has_higher_condition

        # Maybe nest an OR sub-tree instead of a leaf
        if (depth < cfg["max_depth"]
                and rng.random() < cfg["nested_or_probability"]
                and leaf_count[0] < cfg["max_leaf_predicates"] - 4):
            sub = _generate_or(rng, table, index, depth + 1, leaf_count)
            if sub is not None:
                children.append(sub)
                has_higher_condition = True
                continue

        leaf = generate_leaf(rng, col, table, index, skip_scan_bias,
                             allow_omit=False)
        if leaf is not None:
            children.append(leaf)
            leaf_count[0] += 1
            has_higher_condition = True

            # Maybe add a redundant predicate on the same column
            if rng.random() < cfg["redundant_probability"]:
                dup = generate_leaf(rng, col, table, index,
                                    skip_scan_bias=False)
                if dup is not None:
                    children.append(dup)
                    leaf_count[0] += 1

            # Maybe add a contradictory predicate
            if rng.random() < cfg["contradictory_probability"] and col.sql_type != "text":
                # col = X AND col = Y where X != Y
                v1 = random_value(rng, col)
                v2 = random_value(rng, col)
                while v2 == v1:
                    v2 = random_value(rng, col)
                children.append(PredNode(kind=PredKind.LEAF, column=col.name, op="=", value=v1))
                children.append(PredNode(kind=PredKind.LEAF, column=col.name, op="=", value=v2))
                leaf_count[0] += 2

    if not children:
        # Ensure at least one predicate
        col = table.col_by_name(index.columns[0])
        v = random_value(rng, col)
        children.append(PredNode(kind=PredKind.LEAF, column=col.name, op="=", value=v))
        leaf_count[0] += 1

    if len(children) == 1:
        return children[0]
    return PredNode(kind=PredKind.AND, children=children)


def _generate_or(
    rng: random.Random,
    table: TableDef,
    index: IndexDef,
    depth: int,
    leaf_count: List[int],
) -> Optional[PredNode]:
    """Generate an OR node with multiple conjunct branches."""
    cfg = PRED_CONFIG
    n = rng.randint(cfg["or_branches_min"], cfg["or_branches_max"])
    branches: List[PredNode] = []
    for _ in range(n):
        if leaf_count[0] >= cfg["max_leaf_predicates"]:
            break
        conj = generate_conjunct(rng, table, index, depth, leaf_count)
        branches.append(conj)
    if len(branches) < 2:
        return branches[0] if branches else None
    return PredNode(kind=PredKind.OR, children=branches)


def generate_predicate_tree(
    rng: random.Random, table: TableDef, index: IndexDef
) -> PredNode:
    """Generate a complete random predicate tree (top-level OR of ANDs)."""
    leaf_count = [0]
    return _generate_or(rng, table, index, depth=0, leaf_count=leaf_count)


# ---------------------------------------------------------------------------
# SQL rendering
# ---------------------------------------------------------------------------


def render_predicate(node: PredNode, table: TableDef) -> str:
    """Render a PredNode tree to a SQL WHERE clause fragment."""
    if node.kind == PredKind.AND:
        parts = [render_predicate(c, table) for c in node.children]
        return "(" + " AND ".join(parts) + ")"

    if node.kind == PredKind.OR:
        parts = [render_predicate(c, table) for c in node.children]
        return "(" + " OR ".join(parts) + ")"

    # LEAF
    col = table.col_by_name(node.column)

    if node.op == "IS NULL":
        return f"{node.column} IS NULL"
    if node.op == "IS NOT NULL":
        return f"{node.column} IS NOT NULL"

    if node.op == "IN":
        lits = ", ".join(sql_literal(v, col) for v in node.value)
        return f"{node.column} IN ({lits})"

    if node.op == "ROW_CMP":
        col_list = ", ".join(node.row_columns)
        val_list = ", ".join(
            sql_literal(v, table.col_by_name(c))
            for c, v in zip(node.row_columns, node.row_values)
        )
        return f"({col_list}) {node.value} ({val_list})"

    # Simple comparison: =, <, <=, >, >=
    lit = sql_literal(node.value, col, node.cross_type)
    return f"{node.column} {node.op} {lit}"


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def build_queries(
    table: TableDef,
    index: IndexDef,
    pred: PredNode,
    is_desc: bool,
) -> Tuple[str, str, str]:
    """Build the MDAM query, SeqScan query, and EXPLAIN query.

    Returns (mdam_sql, seqscan_sql, explain_sql).
    """
    cols = ", ".join(index.columns)
    where = render_predicate(pred, table)
    direction = "DESC" if is_desc else "ASC"
    order_parts = ", ".join(f"{c} {direction}" for c in index.columns)
    order_clause = f"ORDER BY {order_parts}"

    select_core = f"SELECT {cols} FROM {table.name} WHERE {where} {order_clause}"

    timeout_ms = PRED_CONFIG["statement_timeout_ms"]

    mdam_sql = f"""\
SET LOCAL enable_mdam = on;
SET LOCAL enable_seqscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL statement_timeout = '{timeout_ms}ms';
{select_core}"""

    seqscan_sql = f"""\
SET LOCAL enable_mdam = off;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL statement_timeout = '{timeout_ms}ms';
{select_core}"""

    explain_mdam_sql = f"""\
SET LOCAL enable_mdam = on;
SET LOCAL enable_seqscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL max_parallel_workers_per_gather = 0;
EXPLAIN {select_core}"""

    explain_seqscan_sql = f"""\
SET LOCAL enable_mdam = off;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL max_parallel_workers_per_gather = 0;
EXPLAIN {select_core}"""

    return mdam_sql, seqscan_sql, explain_mdam_sql, explain_seqscan_sql


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

# Sentinel for timeouts / errors
TIMEOUT = "TIMEOUT"
ERROR = "ERROR"


def execute_in_transaction(
    conn, multi_sql: str
) -> Union[List[tuple], str]:
    """Execute a multi-statement SQL block inside a transaction.

    The block should contain SET LOCAL statements followed by a SELECT.
    Returns the result rows, or a sentinel string on error/timeout.
    """
    try:
        with conn.transaction():
            stmts = [s.strip() for s in multi_sql.split(";\n") if s.strip()]
            cur = None
            for stmt in stmts:
                cur = conn.execute(stmt)
            if cur is not None and cur.description is not None:
                return cur.fetchall()
            return []
    except psycopg.errors.QueryCanceled:
        return TIMEOUT
    except Exception as e:
        return f"{ERROR}: {e}"


def check_mdam_used(explain_lines: List[str]) -> Tuple[bool, str]:
    """Check whether the text EXPLAIN plan used MDAM (Append of IndexScans).

    Returns (used_mdam, plan_summary_string).
    """
    has_append = False
    has_index_scan = False
    has_filter = False
    has_non_index_child = False
    first_node = ""

    for line in explain_lines:
        stripped = line.strip()
        # A Filter: line means at least one indexed-column predicate was
        # left as a heap-tuple filter (all test predicates reference indexed
        # columns).  That rules MDAM out: MDAM either pushes everything into
        # index quals on per-retrieval scans, or it doesn't fire at all.
        if stripped.startswith("Filter:"):
            has_filter = True
            continue
        # Extract node type: lines look like "->  Append  (cost=...)" or
        # "->  Index Only Scan ..." or just "Append  (cost=..." at top level
        node = stripped.lstrip("-> ").split("(")[0].split(" using ")[0].strip()
        if not node:
            continue

        if not first_node and node:
            first_node = node

        if node == "Append":
            has_append = True
        elif node in ("Index Scan", "Index Only Scan",
                       "Index Scan Backward", "Index Only Scan Backward"):
            has_index_scan = True
        elif has_append and node not in ("Append",) and "->" in stripped:
            # A child of Append that isn't an index scan
            if node not in ("Index Scan", "Index Only Scan",
                            "Index Scan Backward", "Index Only Scan Backward"):
                has_non_index_child = True

    if has_filter:
        return False, first_node
    if has_append and has_index_scan and not has_non_index_child:
        return True, "Append of IndexScans"
    if not has_append and has_index_scan:
        return True, first_node
    return False, first_node


def extract_plan_info(explain_lines: List[str]) -> Dict[str, Any]:
    """Extract the chosen index, scan direction, and whether a Sort sits
    above the index-scan layer.

    For an Append of IndexScans (MDAM), all children scan the same index in
    the same direction, so any child gives us both.  For a plain index scan,
    the single scan line gives us the answer.  A Sort node anywhere in the
    plan above the first index-scan layer means the post-scan output order
    is enforced by Sort, not by MDAM's natural Append ordering -- the
    natural-ordering check on returned rows is not applicable in that case.
    """
    sort_above = False
    index_name: Optional[str] = None
    direction = "forward"
    for line in explain_lines:
        stripped = line.strip()
        node_text = stripped.lstrip("-> ").strip()
        if not node_text:
            continue
        node = node_text.split("(")[0].strip()
        if index_name is None and node.startswith("Sort"):
            sort_above = True
            continue
        if "Index Only Scan Backward using " in node_text \
                or "Index Scan Backward using " in node_text:
            if index_name is None:
                direction = "backward"
                after = node_text.split("using ", 1)[1]
                index_name = after.split()[0]
        elif "Index Only Scan using " in node_text \
                or "Index Scan using " in node_text:
            if index_name is None:
                direction = "forward"
                after = node_text.split("using ", 1)[1]
                index_name = after.split()[0]
    return {"index_name": index_name, "direction": direction,
            "sort_above": sort_above}


# ---------------------------------------------------------------------------
# Result comparison
# ---------------------------------------------------------------------------


def compare_results(
    mdam_rows: List[tuple], seqscan_rows: List[tuple]
) -> Optional[Dict]:
    """Compare two result sets row-by-row.

    Returns None if identical, or a dict with divergence details.
    """
    if len(mdam_rows) != len(seqscan_rows):
        return {
            "type": "row_count",
            "mdam_count": len(mdam_rows),
            "seqscan_count": len(seqscan_rows),
            "first_extra": (
                mdam_rows[len(seqscan_rows)] if len(mdam_rows) > len(seqscan_rows)
                else seqscan_rows[len(mdam_rows)]
            ),
        }

    for i, (mr, sr) in enumerate(zip(mdam_rows, seqscan_rows)):
        if mr != sr:
            ctx = 5
            return {
                "type": "ordering",
                "position": i,
                "mdam_row": mr,
                "seqscan_row": sr,
                "mdam_context": mdam_rows[max(0, i - ctx) : i + ctx + 1],
                "seqscan_context": seqscan_rows[max(0, i - ctx) : i + ctx + 1],
            }

    return None


def check_rows_sorted(
    rows: List[tuple],
    selected_columns: List[str],
    index_columns: List[str],
    direction: str,
) -> Optional[Dict]:
    """Verify rows are sorted by `index_columns` in the given direction.

    The SELECT list is `selected_columns` (the test's chosen index columns),
    which determines tuple positions; `index_columns` is the column ordering
    of the index actually picked by the planner.  All test indexes are plain
    ASC, NULLS LAST, so a backward scan yields descending values with NULLs
    first.

    Returns None if sorted, or a dict describing the first violation.  If
    `index_columns` references any column absent from `selected_columns`,
    returns None (we can't verify what wasn't selected).
    """
    try:
        key_positions = [selected_columns.index(c) for c in index_columns]
    except ValueError:
        return None

    # ASC index: forward scan => ASC, NULLS LAST.  Backward scan => DESC,
    # NULLS FIRST.  cmp_col(a, b) returns -1 if a should appear earlier in
    # the scan than b, +1 if later, 0 if same position on this column.
    if direction == "backward":
        def cmp_col(a, b):
            if a is None and b is None:
                return 0
            if a is None:
                return -1  # NULLS FIRST: null earlier
            if b is None:
                return 1
            if a > b:
                return -1  # larger value earlier in DESC
            if a < b:
                return 1
            return 0
    else:
        def cmp_col(a, b):
            if a is None and b is None:
                return 0
            if a is None:
                return 1   # NULLS LAST: null later
            if b is None:
                return -1
            if a < b:
                return -1
            if a > b:
                return 1
            return 0

    for i in range(1, len(rows)):
        prev_row, curr_row = rows[i - 1], rows[i]
        for p in key_positions:
            c = cmp_col(prev_row[p], curr_row[p])
            if c < 0:
                break  # correct order; stop comparing further columns
            if c > 0:
                ctx = 5
                return {
                    "type": "natural_ordering",
                    "position": i,
                    "prev_row": prev_row,
                    "curr_row": curr_row,
                    "index_columns": index_columns,
                    "direction": direction,
                    "context": rows[max(0, i - ctx): i + ctx + 1],
                }
            # c == 0: tied on this column, try next
    return None


# ---------------------------------------------------------------------------
# Single test execution
# ---------------------------------------------------------------------------


def run_single_test(
    conn, rng: random.Random, test_id: int, verbose: bool
) -> Dict:
    """Run one fuzz test. Returns a result dict."""
    # Pick table (weighted)
    r = rng.random()
    cumw = 0.0
    table = TABLES[0]
    for t in TABLES:
        cumw += t.weight
        if r <= cumw:
            table = t
            break

    # Pick index
    index = rng.choice(table.indexes)

    # Scan direction
    is_desc = rng.random() < PRED_CONFIG["desc_probability"]
    direction = "DESC" if is_desc else "ASC"

    # Generate a predicate, EXPLAIN it, and retry if MDAM didn't fire
    # (ordering conflict, shattering truncation, etc.).  EXPLAIN is plan-
    # only and cheap; this lets us spend our execution budget on the
    # predicates MDAM actually transforms, without baking MDAM's bail
    # rules into the generator.  If we exhaust retries, we run the last
    # candidate anyway -- it counts as a skip and preserves visibility
    # into the "no good candidate" tail.
    max_retries = PRED_CONFIG["max_explain_retries"]
    pred = None
    mdam_sql = seqscan_sql = explain_mdam_sql = explain_seqscan_sql = None
    where_clause = ""
    explain_text = ""
    used_mdam = False
    plan_summary = ""
    plan_info = {"index_name": None, "direction": "forward",
                 "sort_above": False}
    retries_used = 0
    explain_error: Optional[str] = None

    for attempt in range(max_retries):
        pred = generate_predicate_tree(rng, table, index)
        mdam_sql, seqscan_sql, explain_mdam_sql, explain_seqscan_sql = \
            build_queries(table, index, pred, is_desc)
        where_clause = render_predicate(pred, table)
        retries_used = attempt + 1

        explain_result = execute_in_transaction(conn, explain_mdam_sql)
        if isinstance(explain_result, str):
            # Error or timeout during EXPLAIN -- bail immediately rather
            # than retrying.  EXPLAIN-only is plan work; a failure here
            # means the planner blew up (think DNF expansion OOM /
            # segfault) or the backend went away, neither of which a
            # different predicate is going to fix on the same dead
            # connection.  The worker will surface this as an abort.
            explain_error = explain_result
            break

        explain_error = None
        explain_lines = [row[0] for row in explain_result]
        explain_text = "\n".join(explain_lines)
        used_mdam, plan_summary = check_mdam_used(explain_lines)
        plan_info = extract_plan_info(explain_lines)
        if used_mdam:
            break

    if verbose:
        cols = ", ".join(index.columns)
        order_parts = ", ".join(f"{c} {direction}" for c in index.columns)
        print(f"[{test_id}] SELECT {cols} FROM {table.name} "
              f"WHERE {where_clause} ORDER BY {order_parts}")

    result = {
        "test_id": test_id,
        "table": table.name,
        "index": index.name,
        "where": where_clause,
        "direction": direction,
        "status": "unknown",
        "used_mdam": used_mdam,
        "plan_summary": plan_summary,
        "retries_used": retries_used,
        # SQL texts captured so the failure log can dump the exact
        # statement that errored (the server log truncates DETAIL: at
        # ~4 KiB so we can't recover it from there).
        "mdam_sql": mdam_sql,
        "seqscan_sql": seqscan_sql,
        "explain_mdam_sql": explain_mdam_sql,
        "explain_seqscan_sql": explain_seqscan_sql,
        "explain_mdam": explain_text,
    }

    if explain_error is not None:
        result["status"] = "explain_error"
        result["error"] = explain_error
        return result

    # Run MDAM query
    mdam_rows = execute_in_transaction(conn, mdam_sql)
    if isinstance(mdam_rows, str):
        if mdam_rows == TIMEOUT:
            result["status"] = "timeout"
        else:
            result["status"] = "mdam_error"
            result["error"] = mdam_rows
        return result

    # Run SeqScan query
    seqscan_rows = execute_in_transaction(conn, seqscan_sql)
    if isinstance(seqscan_rows, str):
        if seqscan_rows == TIMEOUT:
            result["status"] = "timeout"
        else:
            result["status"] = "seqscan_error"
            result["error"] = seqscan_rows
        return result

    # Natural-ordering check: when MDAM is used and the planner did not
    # bolt a Sort on top, the returned rows must come back sorted by the
    # index actually picked, in the scan's direction.  MDAM stamps its
    # AppendPath with index pathkeys unconditionally (mdam_add_paths) and
    # bails out entirely when ordering can't be preserved
    # (mdam_detect_ordering_conflict), so this should always hold; a
    # violation means MDAM is claiming pathkeys it doesn't honor.  When a
    # Sort sits above the plan, the post-Sort order tells us nothing
    # about MDAM's natural ordering, so we skip.
    natural_div = None
    if used_mdam and not plan_info["sort_above"] \
            and plan_info["index_name"]:
        chosen_idx_cols = INDEX_COLUMNS_BY_NAME.get(plan_info["index_name"])
        if chosen_idx_cols:
            natural_div = check_rows_sorted(
                mdam_rows, list(index.columns), chosen_idx_cols,
                plan_info["direction"])

    # Compare row content / order against seqscan truth source
    divergence = compare_results(mdam_rows, seqscan_rows)

    # Prefer the natural-ordering diagnosis when both fire
    if natural_div is not None:
        divergence = natural_div

    if divergence is None:
        result["status"] = "pass" if used_mdam else "skip"
        result["row_count"] = len(mdam_rows)
    else:
        result["status"] = "FAIL"
        result["divergence"] = divergence
        result["mdam_row_count"] = len(mdam_rows)
        result["seqscan_row_count"] = len(seqscan_rows)
        result["explain_mdam"] = explain_text
        # Also capture the seqscan plan for the failure log
        seqscan_explain_result = execute_in_transaction(conn, explain_seqscan_sql)
        if isinstance(seqscan_explain_result, str):
            result["explain_seqscan"] = seqscan_explain_result
        else:
            result["explain_seqscan"] = "\n".join(
                row[0] for row in seqscan_explain_result)

    return result


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (name,)
    ).fetchone()
    return row[0]


def setup_tables(conn, verbose: bool = False) -> None:
    """Create test tables if they don't already exist."""
    for table in TABLES:
        if table_exists(conn, table.name):
            if verbose:
                print(f"  Table {table.name} already exists, skipping.")
            continue

        print(f"  Creating table {table.name} ({table.row_count} rows)...")
        conn.execute(f"CREATE TABLE {table.name} ("
                     + ", ".join(f"{c.name} {c.sql_type}" for c in table.columns)
                     + ")")
        # setseed must precede the INSERT in the same transaction so the
        # PRNG stream used by random() inside the INSERT is deterministic.
        conn.execute(f"SELECT setseed({table.seed})")
        conn.execute(table.insert_sql)
        for idx in table.indexes:
            cols = ", ".join(idx.columns)
            conn.execute(f"CREATE INDEX {idx.name} ON {table.name} ({cols})")
        conn.commit()
        # VACUUM ANALYZE: sets the visibility map so index-only scans can
        # actually return tuples without heap fetches.  ANALYZE alone
        # doesn't touch the VM, and an unvacuumed freshly-loaded table
        # forces every "Index Only Scan" plan to fall back to heap probes.
        # VACUUM can't run inside a transaction block.
        conn.autocommit = True
        try:
            conn.execute(f"VACUUM (ANALYZE) {table.name}")
        finally:
            conn.autocommit = False
        print(f"  Table {table.name} ready.")


# ---------------------------------------------------------------------------
# Failure logging
# ---------------------------------------------------------------------------


def format_failure(result: Dict) -> str:
    """Format a failure result for the log file."""
    lines = []
    status = result.get("status", "?")
    lines.append(f"{'=' * 72}")
    header = "FAILURE" if status == "FAIL" else f"ABORT ({status})"
    lines.append(f"{header} test_id={result['test_id']}")
    lines.append(f"Table: {result['table']}  Index: {result['index']}  "
                 f"Direction: {result['direction']}")
    lines.append(f"Plan: {result.get('plan_summary', 'N/A')}  "
                 f"Used MDAM: {result.get('used_mdam', 'N/A')}")
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    lines.append(f"")
    lines.append(f"-- WHERE clause:")
    lines.append(result["where"])
    lines.append(f"")

    # For non-FAIL aborts (server crash, error, timeout-during-explain),
    # dump the offending SQL.  The server log truncates DETAIL: well
    # before the full statement, so this is the only place the full
    # statement survives.
    if status in ("explain_error", "mdam_error", "seqscan_error"):
        # Pick the SQL that actually errored
        sql_key = {
            "explain_error": "explain_mdam_sql",
            "mdam_error": "mdam_sql",
            "seqscan_error": "seqscan_sql",
        }[status]
        sql = result.get(sql_key)
        if sql:
            lines.append(f"-- Offending SQL ({sql_key}):")
            lines.append(sql)
            lines.append(f"")

    div = result.get("divergence", {})
    if div.get("type") == "row_count":
        lines.append(f"-- Row count mismatch:")
        lines.append(f"   MDAM rows:    {div['mdam_count']}")
        lines.append(f"   SeqScan rows: {div['seqscan_count']}")
    elif div.get("type") == "ordering":
        pos = div["position"]
        lines.append(f"-- Ordering divergence at row {pos}:")
        lines.append(f"   MDAM row:    {div['mdam_row']}")
        lines.append(f"   SeqScan row: {div['seqscan_row']}")
        lines.append(f"")
        lines.append(f"-- MDAM context (rows around position {pos}):")
        for j, row in enumerate(div.get("mdam_context", [])):
            lines.append(f"   [{max(0, pos - 5) + j}] {row}")
        lines.append(f"-- SeqScan context:")
        for j, row in enumerate(div.get("seqscan_context", [])):
            lines.append(f"   [{max(0, pos - 5) + j}] {row}")
    elif div.get("type") == "natural_ordering":
        pos = div["position"]
        lines.append(f"-- Natural-order violation at row {pos}")
        lines.append(f"   Chosen index columns: {div['index_columns']}")
        lines.append(f"   Scan direction:       {div['direction']}")
        lines.append(f"   Previous row: {div['prev_row']}")
        lines.append(f"   Current row:  {div['curr_row']}")
        lines.append(f"")
        lines.append(f"-- MDAM context (rows around position {pos}):")
        for j, row in enumerate(div.get("context", [])):
            lines.append(f"   [{max(0, pos - 5) + j}] {row}")

    if result.get("explain_mdam"):
        lines.append(f"")
        lines.append(f"-- MDAM EXPLAIN:")
        lines.append(result["explain_mdam"])

    if result.get("explain_seqscan"):
        lines.append(f"")
        lines.append(f"-- SeqScan EXPLAIN (oracle):")
        lines.append(result["explain_seqscan"])

    lines.append(f"{'=' * 72}")
    lines.append(f"")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------


def worker_main(
    seed: int,
    duration: float,
    worker_id: int,
    verbose: bool,
    counter,
    fail_counter,
    skip_counter,
    timeout_counter,
    nonempty_pass_counter,
    lock,
    failure_log_path: str,
    abort_event,
) -> None:
    """Main loop for a single worker process."""
    rng = random.Random(f"{seed}:{worker_id}")
    # Connect inside a try so a dead server at startup also triggers
    # abort rather than wedging the worker.
    try:
        conn = psycopg.connect(**DB_PARAMS, autocommit=True)
    except Exception as exc:
        with lock:
            if not abort_event.is_set():
                abort_event.set()
                print(f"\n*** ABORT worker {worker_id}: cannot connect: "
                      f"{exc} ***", file=sys.stderr)
        return

    start = time.monotonic()
    local_test_id = 0

    try:
        while time.monotonic() - start < duration:
            if abort_event.is_set():
                break
            local_test_id += 1
            global_id = worker_id * 10_000_000 + local_test_id

            # run_single_test catches DB exceptions via
            # execute_in_transaction; any uncaught exception here is a
            # harness bug, but still worth treating as abort-worthy.
            try:
                result = run_single_test(conn, rng, global_id, verbose)
            except Exception as exc:
                with lock:
                    if not abort_event.is_set():
                        abort_event.set()
                        print(f"\n*** ABORT worker {worker_id} test_id="
                              f"{global_id}: unexpected exception: "
                              f"{exc} ***", file=sys.stderr)
                break

            with lock:
                counter.value += 1
                status = result["status"]
                if status == "FAIL":
                    fail_counter.value += 1
                    with open(failure_log_path, "a") as f:
                        f.write(format_failure(result))
                    # Also print to stderr
                    print(f"\n*** FAILURE test_id={global_id} "
                          f"table={result['table']} "
                          f"index={result['index']} ***",
                          file=sys.stderr)
                    print(f"    WHERE {result['where']}", file=sys.stderr)
                    div = result.get("divergence", {})
                    if div.get("type") == "row_count":
                        print(f"    Row count: MDAM={div['mdam_count']} "
                              f"SeqScan={div['seqscan_count']}",
                              file=sys.stderr)
                    elif div.get("type") == "ordering":
                        print(f"    Divergence at row {div['position']}: "
                              f"MDAM={div['mdam_row']} "
                              f"SeqScan={div['seqscan_row']}",
                              file=sys.stderr)
                    elif div.get("type") == "natural_ordering":
                        print(f"    Natural-order violation at row "
                              f"{div['position']} on index columns "
                              f"{div['index_columns']} "
                              f"({div['direction']}): "
                              f"prev={div['prev_row']} "
                              f"curr={div['curr_row']}",
                              file=sys.stderr)
                elif status in ("explain_error", "mdam_error",
                                 "seqscan_error"):
                    # Any database error (segfault, OOM, syntax, etc.)
                    # aborts the run.  Capture full details to the
                    # failure log AND stderr so the user can reproduce.
                    if not abort_event.is_set():
                        abort_event.set()
                        with open(failure_log_path, "a") as f:
                            f.write(format_failure(result))
                        print(f"\n*** ABORT test_id={global_id} "
                              f"status={status} "
                              f"table={result['table']} "
                              f"index={result['index']} ***",
                              file=sys.stderr)
                        print(f"    error: {result.get('error', '?')}",
                              file=sys.stderr)
                        print(f"    WHERE {result['where']}",
                              file=sys.stderr)
                        sql_key = {
                            "explain_error": "explain_mdam_sql",
                            "mdam_error": "mdam_sql",
                            "seqscan_error": "seqscan_sql",
                        }[status]
                        sql = result.get(sql_key)
                        if sql:
                            print(f"    Full SQL ({sql_key}):",
                                  file=sys.stderr)
                            for line in sql.splitlines():
                                print(f"      {line}", file=sys.stderr)
                    break
                elif status == "pass":
                    if result.get("row_count", 0) > 0:
                        nonempty_pass_counter.value += 1
                elif status == "skip":
                    skip_counter.value += 1
                elif status == "timeout":
                    timeout_counter.value += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="MDAM OR-clause optimizer fuzz tester"
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed (default: current time)")
    parser.add_argument("--duration", type=int, default=300,
                        help="Duration in seconds (default: 300)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: CPU count)")
    parser.add_argument("--setup-only", action="store_true",
                        help="Only create/load tables, don't run tests")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each query as it runs")
    parser.add_argument("--no-redundant", action="store_true",
                        help="Disable redundant predicate generation")
    parser.add_argument("--no-contradictory", action="store_true",
                        help="Disable contradictory predicate generation")
    args = parser.parse_args()

    if args.no_redundant:
        PRED_CONFIG["redundant_probability"] = 0.0
    if args.no_contradictory:
        PRED_CONFIG["contradictory_probability"] = 0.0

    seed = args.seed if args.seed is not None else int(time.time())
    workers = args.workers if args.workers is not None else os.cpu_count()
    duration = args.duration

    print(f"MDAM Fuzz Tester")
    print(f"  Seed:     {seed}")
    print(f"  Workers:  {workers}")
    print(f"  Duration: {duration}s")
    print()

    # Setup tables
    print("Setting up tables...")
    conn = psycopg.connect(**DB_PARAMS, autocommit=False)
    try:
        setup_tables(conn, verbose=True)
    finally:
        conn.close()
    print()

    if args.setup_only:
        print("Setup complete (--setup-only).")
        return

    # Prepare output directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "mdam_fuzz_output")
    os.makedirs(output_dir, exist_ok=True)

    # Prepare shared state
    counter = multiprocessing.Value("i", 0)
    fail_counter = multiprocessing.Value("i", 0)
    skip_counter = multiprocessing.Value("i", 0)
    timeout_counter = multiprocessing.Value("i", 0)
    nonempty_pass_counter = multiprocessing.Value("i", 0)
    lock = multiprocessing.Lock()
    abort_event = multiprocessing.Event()
    failure_log_path = os.path.join(output_dir, f"mdam_fuzz_failures_{seed}.log")

    print(f"Running tests (failure log: {failure_log_path})...")
    print()

    start = time.monotonic()

    processes = []
    for wid in range(workers):
        p = multiprocessing.Process(
            target=worker_main,
            args=(seed, duration, wid, args.verbose,
                  counter, fail_counter, skip_counter, timeout_counter,
                  nonempty_pass_counter, lock, failure_log_path,
                  abort_event),
        )
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

    elapsed = time.monotonic() - start
    total = counter.value
    fails = fail_counter.value
    skips = skip_counter.value
    timeouts = timeout_counter.value
    passes = total - fails - skips - timeouts
    nonempty = nonempty_pass_counter.value
    empty = passes - nonempty
    nonempty_pct = (100.0 * nonempty / passes) if passes else 0.0

    print()
    print(f"{'=' * 50}")
    print(f"MDAM Fuzz Test Results")
    print(f"{'=' * 50}")
    print(f"  Seed:              {seed}")
    print(f"  Workers:           {workers}")
    print(f"  Elapsed:           {elapsed:.1f}s")
    print(f"  Total tests:       {total}")
    print(f"  Passed:            {passes}")
    print(f"    >=1 row:         {nonempty} ({nonempty_pct:.1f}%)")
    print(f"    0 rows:          {empty}")
    print(f"  Failed:            {fails}")
    print(f"  Skipped (no MDAM): {skips}")
    print(f"  Timeouts:          {timeouts}")
    print(f"{'=' * 50}")

    if abort_event.is_set():
        print(f"\n*** ABORTED: a query errored / the server crashed -- "
              f"see {failure_log_path} ***")
        sys.exit(2)
    if fails > 0:
        print(f"\n*** {fails} FAILURE(S) -- see {failure_log_path} ***")
        sys.exit(1)
    else:
        print(f"\nAll tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
