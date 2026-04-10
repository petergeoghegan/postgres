#!/usr/bin/env python3
"""
General OR Optimization / MDAM Transformation Prototype
=======================================================

Simulates the "general OR transformation" technique from Tandem's 1995
paper "Efficient Search of Multidimensional B-Trees", adapted
to an executor architecture where the output is a set of independent index
scan retrievals concatenated via UNION ALL (rather than a single scan
driven by a GEM-tree at run time).

Algorithm Overview
------------------
The transformation takes an arbitrary boolean predicate and a multi-column
index, and produces a set of non-overlapping retrieval paths.  Each path
can be executed as a single btree index scan.  The paths are ordered so
their concatenation produces rows in index key space order.

The pipeline has four steps, executed in order by generate_mdam_retrieval_clauses():

  Step 1: DNF conversion + simplification
          to_dnf() + simplify_disjunct()

    Convert the predicate tree into Disjunctive Normal Form (OR of ANDs of
    atoms).  Each conjunction is immediately simplified: contradictions are
    detected (dept=5 AND dept=10), overlapping ranges are intersected
    (dept>3 AND dept>7 -> dept>7), EQ/IN are intersected, and empty integer
    intervals are pruned.

    Corresponds to the paper's "Table 2" disjuncts.

  Step 2: Generate initial retrievals (shattering)
          generate_retrievals_recursive()
          using: get_critical_points_for_column(), generate_elementary_intervals()

    For each index column (left to right), collect all boundary values
    ("critical points") from the DNF, then partition the column's value
    space into elementary intervals (<a, =a, (a,b), =b, >b).  Recursively
    enumerate all combinations across columns.  At the leaf, filter: keep
    only paths that satisfy at least one original DNF clause (via
    check_retrieval_satisfies_original_dnf / check_atom_compatibility).

    This guarantees non-overlapping paths (no duplicate rows) but produces
    far more paths than necessary -- the next step merges them.

    Roughly corresponds to the paper's "Table 3" retrievals.

  Step 3: Merge retrievals
          merge_retrievals()
          using: _extract_effective_interval(), _merge_interval_list(),
                 _convert_interval_to_atoms(), _merge_eq_to_in()

    For each column in *reverse* index order, group paths by their "base"
    (atoms on all other columns), then:
      Stage 1: Merge overlapping/adjacent intervals (interval coalescing)
      Stage 2: Fold col=v1 + col=v2 into col IN (v1,v2)

    Reverse order maximizes merging: trailing columns are simplified first,
    so leading columns are more likely to share identical bases.

  Step 4: Expand leading constraints, sort, and coalesce
          expand_and_sort_leading_constraints()
          coalesce_adjacent_path_ranges()
          using: get_path_sort_key(), _one_pass_coalesce_adjacent_path_ranges()

    4a. For each path, if a leading column has an IN or range AND a later
        column is also constrained, expand the leading column into separate
        paths (IN -> individual EQs, ranges -> elementary intervals).  This
        is necessary because a single btree scan with "dept IN (2,4) AND
        item_class=5" doesn't produce rows in (dept,sdate,item_class,store)
        order -- the sdate values for dept=2 and dept=4 interleave.

    4b. Sort all expanded paths by index key space position.

    4c. Coalesce: merge adjacent sorted paths that share the same base and
        have contiguous intervals on some column.  This recovers paths that
        were needlessly shattered by 4a.

  After all four steps, detect_ordering_conflict() checks whether the
  result is valid for ordered execution.

Ordering Conflict (Fundamental Limitation)
------------------------------------------
When two adjacent paths share a non-point constraint on some leading column
L (e.g. dept > 10) but differ on a later column M (e.g. one has
item_class=5, the other has store=50), rows from the two paths would need
to be interleaved by L to maintain overall order.  UNION ALL just
concatenates, so this is impossible.

This is a fundamental limitation of the "independent scans + Append"
architecture.  The original Tandem system avoided it by generating keys
one at a time from a GEM-tree during a single scan, so ordering was
inherent.  Test cases marked expected_ordering_failure=True demonstrate
predicates where this limitation applies.

Relationship to Tandem GEM design
---------------------------------
This script solves the same problem but with a different mechanism:

  - Tandem builds a per-column GEM-tree with reference sets (disjunct
    bitmasks) and traverses it at run time, generating keys one at a time.
    Ordering is inherent in the traversal.

  - This script generates complete retrieval paths at compile time.  Each
    path becomes an independent index scan.  Ordering must be explicitly
    ensured by expanding leading constraints and sorting.

  - Tandem's "sparse algorithm" (probing the index for the next actual
    value in a range) has no analog here -- PostgreSQL's btree machinery
    handles this within each individual scan.

  - The ordering conflict problem is unique to this script's architecture.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from enum import Enum
import re

import psycopg2
import sqlparse

ELEVEL = 1 # Set to 1 to enable debug prints, 2 for more verbose if needed
DEBUG_DONT_QUERY = False # Set to True to not actually execute queries
DB_CONNECTION_PARAMS  = {"host": "localhost", "dbname": "regression", "user": "pg"}
TRANSFORMED_SQL_LOG_FILE_PATH = "mdam_transformed_queries.log"

# Instructions to generate "sales_mdam_paper", which this script expects will be
# present in whatever database it connects to:
'''
drop table if exists sales_mdam_paper;
create unlogged table sales_mdam_paper
(
  dept int4,
  sdate date,
  item_class serial,
  store int4,
  item int4,
  total_sales numeric
);

-- 900 hundred million rows in total here:
insert into sales_mdam_paper (dept, sdate, item_class, store, total_sales)
select
  dept,
  '1995-01-01'::date + sdate,
  item_class,
  store,
  (random() * 500.0) as total_sales
from
  -- "So let us assume that the values for the column dept in the table range from 1 through 100":
  generate_series(1, 100) dept,
  -- 400 days, starting on Jan 2 of 95 (arbitrary):
  generate_series(1, 400) sdate,
  -- Highest item_class in paper is 50, so arbitrarily assume 75 total:
  generate_series(1, 75) item_class,
  -- Highest store in paper is 250, so arbitrarily assume 300 total:
  generate_series(1, 300) store;

-- Duration of INSERT on my workstation:
--
-- INSERT 0 900000000
-- Time: 900564.138 ms (15:00.564)
--
--                                         List of relations
-- ┌────────┬──────────────────┬───────┬───────┬─────────────┬───────────────┬───────┬─────────────┐
-- │ Schema │       Name       │ Type  │ Owner │ Persistence │ Access method │ Size  │ Description │
-- ├────────┼──────────────────┼───────┼───────┼─────────────┼───────────────┼───────┼─────────────┤
-- │ public │ sales_mdam_paper │ table │ pg    │ unlogged    │ heap          │ 51 GB │ ∅           │
-- └────────┴──────────────────┴───────┴───────┴─────────────┴───────────────┴───────┴─────────────┘
-- (1 row)

-- Build index, using the column order from the paper:
create index mdam_idx on sales_mdam_paper(dept, sdate, item_class, store);
'''

def print_debug(message, level=1):
    if ELEVEL >= level:
        print(f"DEBUG{level}: {message}")

class Operator(Enum):
    EQ = '='
    LT = '<'
    LE = '<='
    GT = '>'
    GE = '>='
    IN = 'IN'
    RANGE_EE = 'RANGE_EE'  # Exclusive-Exclusive range (low, high)
    IS_ANYTHING = 'IS_ANYTHING' # Special marker for an unconstrained interval

    def __str__(self):
        return self.value # Allows direct use in f-strings for SQL

# --- Expression Classes (Atom, AndOp, OrOp) ---
class Expression:
    def to_sql(self):
        raise NotImplementedError
    def __repr__(self):
        return self.__class__.__name__

class Atom(Expression):
    def __init__(self, column: str, operator: Operator, value):
        self.column = column
        if not isinstance(operator, Operator):
            raise ValueError(f"Operator must be an instance of Operator Enum, got {type(operator)}")
        self.operator = operator
        self.value = value
    def __repr__(self):
        return f"Atom({self.column} {self.operator.value} {repr(self.value)})"
    def __eq__(self, other):
        return isinstance(other, Atom) and \
                self.column == other.column and \
                self.operator == other.operator and \
                self.value == other.value
    def __hash__(self):
        val = self.value
        if isinstance(val, list):
            val = tuple(sorted(val))
        return hash(("Atom", self.column, self.operator.name, val)) # Use .name for stable hash

    @staticmethod
    def value_to_sql_literal(value_item):
        """Converts a Python value to its SQL literal representation."""
        if isinstance(value_item, (str, Decimal, date)):
            return f"'{str(value_item).replace("'", "''")}'"
        return str(value_item) # For int, float, bool etc.

    def to_sql(self):
        if self.operator == Operator.IN:
            if not self.value or not isinstance(self.value, (list,tuple)):
                return "1=0"
            qv = tuple(Atom.value_to_sql_literal(v_item) for v_item in self.value)
            if not qv:
                return "1=0"
            return f"{self.column} {self.operator.value} ({', '.join(qv)})"
        if self.operator == Operator.RANGE_EE:
            if not (isinstance(self.value, tuple) and len(self.value)==2):
                return "1=0"
            sql = (f'({self.column} {Operator.GT.value} {Atom.value_to_sql_literal(self.value[0])} AND '
                   f'{self.column} {Operator.LT.value} {Atom.value_to_sql_literal(self.value[1])})')
            return sql
        return f"{self.column} {self.operator.value} {Atom.value_to_sql_literal(self.value)}"

class AndOp(Expression):
    def __init__(self, operands: list):
        self.operands = operands
    def __repr__(self):
        if not self.operands:
            return "TRUE_AND"
        return " AND ".join(f"({op})" if isinstance(op, OrOp) else str(op) for op in self.operands)
    def __eq__(self, other):
        if not isinstance(other, AndOp):
            return False
        return self.operands == other.operands
    def __hash__(self):
        if all(isinstance(op, Atom) for op in self.operands):
            return hash(("AndOp", tuple(self.operands)))
        raise TypeError(f"General AndOp cannot be reliably hashed if operands are not canonical Atoms: {self.operands}")
    def to_sql(self):
        if not self.operands:
            return "1=1"
        parts = []
        for op in self.operands:
            sql = op.to_sql()
            if isinstance(op, OrOp) and len(op.operands) > 1:
                sql = f"({sql})"
            parts.append(sql)
        return " AND ".join(parts)

class OrOp(Expression):
    def __init__(self, operands: list):
        self.operands = operands
        self._sorted_dnf_clauses_for_hash = None
    def __repr__(self):
        if not self.operands:
            return "FALSE_OR"
        return " OR ".join(f"({op})" \
                if isinstance(op, AndOp) else str(op) for op in self.operands)
    def __eq__(self, other):
        if not isinstance(other, OrOp):
            return False
        return self.operands == other.operands
    def __hash__(self):
        if self._sorted_dnf_clauses_for_hash is not None:
            return hash(("OrOp", self._sorted_dnf_clauses_for_hash))
        raise TypeError("General OrOp not hashable unless it's canonical DNF or empty.")
    def to_sql(self):
        if not self.operands:
            return "1=0"

        sql_parts = []
        for op in self.operands:
            op_sql_val = op.to_sql()
            # An operand 'op' of an OrOp needs parentheses if 'op' is an AndOp
            # with more than one actual condition.
            # If 'op' is an AndOp with 0 or 1 condition, or if 'op' is an Atom or another OrOp,
            # its own .to_sql() result is sufficient (OrOp is associative, Atom is simple).
            if isinstance(op, AndOp) and len(op.operands) > 1:
                sql_parts.append(f"({op_sql_val})")
            else:
                sql_parts.append(op_sql_val)
        return " OR ".join(sql_parts)

def _atom_sort_key(a: Atom) -> tuple:
    """Canonical sort key for atoms within a path."""
    return (a.column, a.operator.name, str(a.value))

def _sort_path(atoms: list[Atom]) -> list[Atom]:
    """Return atoms sorted in canonical order."""
    return sorted(atoms, key=_atom_sort_key)

def _merge_eq_to_in(paths: list[list[Atom]], col_name: str,
                    adjacent_only: bool = False) -> list[list[Atom]]:
    """Merge paths that differ only in an EQ value on col_name into IN lists.

    For paths sharing the same "base" (all atoms except the EQ on col_name),
    combines col = v1, col = v2, ... into col IN (v1, v2, ...).

    If adjacent_only is True, only merges consecutive paths (preserving sort
    order for the coalescing pass).  Otherwise scans all remaining paths.
    """
    result = []
    used = [False] * len(paths)
    for i, path in enumerate(paths):
        if used[i]:
            continue
        eq_atom = next((a for a in path if a.column == col_name and a.operator == Operator.EQ), None)
        if eq_atom is None:
            result.append(path)
            used[i] = True
            continue
        base = frozenset(a for a in path if a is not eq_atom)
        values = {eq_atom.value}
        used[i] = True
        for j in range(i + 1, len(paths)):
            if used[j]:
                continue
            other_eq = next((a for a in paths[j] if a.column == col_name and a.operator == Operator.EQ), None)
            if other_eq is None:
                if adjacent_only:
                    break
                continue
            other_base = frozenset(a for a in paths[j] if a is not other_eq)
            if other_base == base:
                values.add(other_eq.value)
                used[j] = True
            elif adjacent_only:
                break
        sorted_vals = sorted(values)
        merged_atom = Atom(col_name, Operator.EQ, sorted_vals[0]) if len(sorted_vals) == 1 \
            else Atom(col_name, Operator.IN, sorted_vals)
        result.append(_sort_path(list(base) + [merged_atom]))
    return result

def generate_mdam_retrieval_clauses(orig_predicate: Expression,
                                    index_cols_list: list[str]) -> tuple[list[str], str | None, int]:
    """
    Transforms a predicate object into a list of SQL WHERE clauses for MDAM-style UNION ALL queries.

    Returns:
        transformed_atoms, a list of transformed and simplified DNF retrievals.
        Might have ordering conflicts (caller needs to check for that themselves).
    """

    def merge_retrievals(current_paths: list, index_column_order: list) -> list[list[Atom]]:
        """Interval coalescing and EQ-to-IN merging across all index columns.

        For each column (in reverse index order), groups paths by their "base"
        (atoms on other columns), merges overlapping intervals on the target
        column, then folds col=v1, col=v2 into col IN (v1,v2).
        """
        for col in reversed(index_column_order):
            # Stage 1: Interval coalescing -- group by base, merge intervals
            grouped = {}   # base_key -> list of intervals
            base_map = {}  # base_key -> base atom list
            for path in current_paths:
                base = _sort_path([a for a in path if a.column != col])
                key = tuple(base)
                base_map[key] = base
                interval = _extract_effective_interval(
                    [a for a in path if a.column == col], col)
                if interval:
                    grouped.setdefault(key, []).append(interval)

            coalesced = []
            for key, intervals in grouped.items():
                for merged_iv in _merge_interval_list(intervals):
                    coalesced.append(_sort_path(
                        list(base_map[key]) + _convert_interval_to_atoms(merged_iv, col)))
            current_paths = coalesced

            # Stage 2: EQ-to-IN merging
            current_paths = _merge_eq_to_in(current_paths, col)

        return current_paths

    def _one_pass_coalesce_adjacent_path_ranges(sorted_paths: list[list[Atom]],
                                                index_cols: list[str]) -> list[list[Atom]]:
        """Try to merge consecutive sorted paths whose intervals on some
        column are contiguous (and whose other atoms are identical)."""
        if len(sorted_paths) < 2:
            return sorted_paths

        result = []
        i = 0
        while i < len(sorted_paths):
            best_merged = None
            best_consumed = 0

            # Try each column as the merge dimension
            for col in index_cols:
                base_list = _sort_path([a for a in sorted_paths[i] if a.column != col])
                base_set = frozenset(base_list)
                col_atoms = [a for a in sorted_paths[i] if a.column == col]
                interval = _extract_effective_interval(col_atoms, col)
                if interval is None:
                    continue

                intervals = [interval]
                last_j = i

                for j in range(i + 1, len(sorted_paths)):
                    j_base = frozenset(_sort_path([a for a in sorted_paths[j] if a.column != col]))
                    if j_base != base_set:
                        break
                    j_interval = _extract_effective_interval(
                        [a for a in sorted_paths[j] if a.column == col], col)
                    if j_interval is None:
                        break
                    if len(_merge_interval_list(intervals + [j_interval])) == 1:
                        intervals.append(j_interval)
                        last_j = j
                    else:
                        break

                consumed = last_j - i + 1
                if consumed > 1 and consumed > best_consumed:
                    merged_intervals = _merge_interval_list(list(intervals))
                    if len(merged_intervals) == 1:
                        new_atoms = _convert_interval_to_atoms(merged_intervals[0], col)
                        best_merged = _sort_path(base_list + new_atoms)
                        best_consumed = consumed

            if best_merged and best_consumed > 1:
                result.append(best_merged)
                i += best_consumed
            else:
                result.append(sorted_paths[i])
                i += 1

        return result

    # Helper function for core logic, below
    def coalesce_adjacent_path_ranges(expanded_retrievals: list[list[Atom]],
                                      index_cols: list[str],
                                      level) -> list[list[Atom]]:
        """
        Combines contiguous ranges that might have been produced by a prior
        expand_and_sort_leading_constraints call

        For example, without this transformation the "MDAM Paper General OR
        Example" test case outputs the following retrievals (among others):

        WHERE dept = 2
          AND sdate < '1995-06-04'
          AND item_class IN (5, 10))

        WHERE dept = 2
          AND sdate = '1995-06-04'
          AND item_class IN (5, 10))

        WHERE dept = 2
          AND (sdate > '1995-06-04'
               AND sdate < '1995-06-25')
          AND item_class IN (5, 10))

        WHERE dept = 2
          AND sdate = '1995-06-25'
          AND item_class IN (5, 10))

        WHERE dept = 2
          AND sdate > '1995-06-25'
          AND item_class IN (5, 10))

        Whereas with this, they can all be coalesced into one "dept = 2" retrieval:

        WHERE dept = 2
          AND item_class IN (5, 10))
        """
        while True:
            merged_one_pass = _one_pass_coalesce_adjacent_path_ranges(expanded_retrievals, index_cols)
            if merged_one_pass == expanded_retrievals: # Check content too
                break
            expanded_retrievals = merged_one_pass

        unique_remerged_retrievals = []
        for atoms_list in expanded_retrievals:
            temp_and_op = AndOp(atoms_list)
            simplified_and_op = simplify_disjunct(temp_and_op, level)
            if simplified_and_op is not None:
                unique_remerged_retrievals.append(simplified_and_op.operands)
            else:
                print_debug(f"Discarding contradictory retrieval after merging/expansion: {atoms_list}",
                            level)

        # Iteratively apply EQ-to-IN merging on all columns until stable.
        # Use adjacent_only=True since paths are sorted and order matters.
        while True:
            merged = unique_remerged_retrievals
            for col in index_cols:
                merged = _merge_eq_to_in(merged, col, adjacent_only=True)
            if merged == unique_remerged_retrievals:
                break
            unique_remerged_retrievals = merged

        return unique_remerged_retrievals

    # =====================================================================
    # Pipeline execution -- see module docstring for step descriptions
    # =====================================================================
    level = 2

    # --- Step 1: DNF conversion + simplification ---
    # to_dnf() converts the arbitrary predicate tree into OR-of-ANDs form,
    # calling simplify_disjunct() on each conjunction to prune contradictions.
    # Output corresponds to the MDAM paper's "Table 2" disjuncts.
    dnf_predicate = to_dnf(orig_predicate, level)

    formatted_sql_display = sqlparse.format(dnf_predicate.to_sql(),
                                            reindent=True,
                                            keyword_case='upper',
                                            indent_width=2,
                                            wrap_after=120)
    print(f"\nBasic DNF-transformed predicate: \n{formatted_sql_display}\n")

    # --- Step 2: Generate initial retrievals (shattering) ---
    # generate_retrievals_recursive() partitions the index key space into
    # elementary intervals per column, enumerates all combinations, and
    # filters to keep only those satisfying at least one DNF clause.
    # Output roughly corresponds to the paper's "Table 3" retrievals.
    # Guarantees no duplicate rows, but produces many more paths than needed.
    initial_retrievals = generate_retrievals_recursive(dnf_predicate,
                                                       dnf_predicate,
                                                       index_cols_list, [],
                                                       level)
    print(f"{len(initial_retrievals)} initial retrievals generated.")

    # --- Step 3: Merge retrievals ---
    # merge_retrievals() reduces path count by coalescing intervals and
    # folding EQ values into IN lists, processing columns in reverse order.
    merged_retrievals = merge_retrievals(initial_retrievals, index_cols_list)
    print(f"{len(merged_retrievals)} retrievals after merging.")

    # --- Step 4a: Expand leading constraints + sort ---
    # expand_and_sort_leading_constraints() shatters leading IN/range
    # constraints so each path has point constraints on leading columns,
    # enabling proper index key space ordering across paths.
    expanded_retrievals = expand_and_sort_leading_constraints(dnf_predicate,
                                                              merged_retrievals,
                                                              index_cols_list,
                                                              level)
    print(f"{len(expanded_retrievals)} retrievals after expanding on leading constraints and sorting.")

    # --- Step 4b: Coalesce adjacent paths ---
    # coalesce_adjacent_path_ranges() merges back adjacent sorted paths that
    # share the same base and have contiguous intervals, recovering paths
    # that were needlessly shattered by Step 4a.
    coalesced_retrievals = coalesce_adjacent_path_ranges(expanded_retrievals,
                                                         index_cols_list,
                                                         level)
    print(f"{len(coalesced_retrievals)} retrievals after final adjacent path range coalescing.")

    return coalesced_retrievals

# =====================================================================
# Step 1 functions: DNF conversion and simplification
# =====================================================================
def to_dnf(expr: Expression, level) -> OrOp:
    print_debug(f"to_dnf called with: {repr(expr)}", level)
    if isinstance(expr, Atom):
        return OrOp([AndOp([expr])])
    if isinstance(expr, OrOp):
        all_clauses = []
        for op_idx, op in enumerate(expr.operands):
            print_debug(f"  to_dnf: OrOp operand {op_idx}: {repr(op)}", level)
            dnf_of_op = to_dnf(op, level)
            assert dnf_of_op.operands is not None
            all_clauses.extend(dnf_of_op.operands)
        return OrOp(all_clauses)

    # Must be an AndOp
    assert isinstance(expr, AndOp)

    res_clauses = [AndOp([])]
    for op_idx, op_expr in enumerate(expr.operands):
        print_debug(f"  to_dnf: AndOp operand {op_idx}: {repr(op_expr)}", level)
        op_dnf = to_dnf(op_expr, level)
        assert op_dnf.operands is not None
        new_res_clauses = []
        for r_clause in res_clauses:
            for o_clause in op_dnf.operands:
                assert all(isinstance(a, Atom) for a in r_clause.operands)
                assert all(isinstance(a, Atom) for a in o_clause.operands)

                combined_atoms = r_clause.operands + o_clause.operands
                combined_and_op = AndOp(combined_atoms)
                simplified_and_op = simplify_disjunct(combined_and_op, level)
                if simplified_and_op is not None: # if not contradictory
                    new_res_clauses.append(simplified_and_op)
        if not new_res_clauses:
            return OrOp([])
        res_clauses = new_res_clauses
    return OrOp(res_clauses)

def simplify_disjunct(and_op: AndOp, level) -> AndOp | None:

    # must be a disjunction of atoms (where Operator.IN counts as an atom)
    assert all(isinstance(a, Atom) for a in and_op.operands)

    final_atoms_for_clause = []
    atoms_by_col = {}
    for atom in and_op.operands:
        atoms_by_col.setdefault(atom.column, []).append(atom)
    for col_name, atoms_on_col_list_for_col in atoms_by_col.items():
        unique_atoms_for_this_col = []
        seen_atom = set()
        for atom_cand in atoms_on_col_list_for_col:
            if atom_cand not in seen_atom:
                unique_atoms_for_this_col.append(atom_cand)
                seen_atom.add(atom_cand)

        # Separate atoms into discrete (EQ, IN) and range types
        all_eq_values = set()
        # For IN lists: start with a sentinel that represents "all values allowed by INs so far"
        # If no IN atoms, it remains None. If one IN atom, it becomes set(IN_list_values).
        # If multiple IN atoms, it's the intersection of their value sets.
        final_in_values_intersection = None
        initial_range_atoms = []

        for atom in unique_atoms_for_this_col:
            if atom.operator == Operator.EQ:
                all_eq_values.add(atom.value)
            elif atom.operator == Operator.IN:
                assert isinstance(atom.value, (list, tuple))
                current_in_set = set(atom.value)
                if final_in_values_intersection is None:
                    final_in_values_intersection = current_in_set
                else:
                    final_in_values_intersection.intersection_update(current_in_set)
                if not final_in_values_intersection: # Intersection became empty
                    print_debug(f"Contradiction: Intersection of IN lists for {col_name} is empty. "
                                f"Atoms: {unique_atoms_for_this_col}",
                                level)
                    return None
            else: # LT, GT, LE, GE, RANGE_EE, IS_ANYTHING
                initial_range_atoms.append(atom)

        # Check for contradiction from multiple distinct EQ values
        if len(all_eq_values) > 1:
            print_debug(f"Contradiction: Multiple distinct EQ values for {col_name}: "
                        f"{all_eq_values}",
                        level)
            return None

        # Determine the set of candidate points from EQ and IN constraints
        candidate_points = None
        if all_eq_values: # len(all_eq_values) is 0 or 1 here
            the_eq_value = list(all_eq_values)[0] if all_eq_values else None
            if final_in_values_intersection is not None:
                if the_eq_value is not None and the_eq_value in final_in_values_intersection:
                    candidate_points = {the_eq_value}
                else: # EQ value not in IN list, or no EQ value but IN list exists
                    candidate_points = set() # Leads to contradiction if IN list was the only source
            elif the_eq_value is not None: # Only EQ constraint
                candidate_points = {the_eq_value}
            # If no EQ and no IN, candidate_points remains None
        elif final_in_values_intersection is not None: # Only IN constraints
            candidate_points = final_in_values_intersection

        def _point_satisfies_interval(point, interval: tuple) -> bool:
            lo, hi, lo_incl, hi_incl = interval
            if lo is not None and (point < lo if lo_incl else point <= lo):
                return False
            if hi is not None and (point > hi if hi_incl else point >= hi):
                return False
            return True

        # Now, process range atoms.
        # If we have candidate_points, filter them using the range atoms.
        # Otherwise, the range atoms define the constraint for this column.
        if candidate_points is not None:
            # Filter candidate_points by the effective interval of initial_range_atoms
            if initial_range_atoms: # Only filter if there are range atoms
                range_interval_for_filtering = _extract_effective_interval(initial_range_atoms, col_name)

                points_kept_after_ranges = {p for p in candidate_points if _point_satisfies_interval(p, range_interval_for_filtering)}

                if not points_kept_after_ranges:
                    print_debug(f"Contradiction: Discrete points {candidate_points} for {col_name} "
                                f"all filtered out by ranges {initial_range_atoms}",
                                level)
                    return None
                candidate_points = points_kept_after_ranges

            # Convert the final set of points to EQ or IN atom(s)
            if len(candidate_points) == 1:
                final_atoms_for_clause.append(Atom(col_name, Operator.EQ, list(candidate_points)[0]))
            elif len(candidate_points) > 1 : # Must be > 0 due to checks above
                final_atoms_for_clause.append(Atom(col_name, Operator.IN, sorted(list(candidate_points))))
            # If candidate_points is empty here, it means contradiction (already handled)
        else: # No discrete constraints initially (no EQs, no INs), only range atoms (or no atoms at all for this col)
            effective_range_interval = \
                _extract_effective_interval(initial_range_atoms,
                                            col_name) # initial_range_atoms might be empty
            if effective_range_interval is None: # Contradiction from range atoms
                print_debug(f"Contradiction from range atoms for {col_name}: "
                            f"{initial_range_atoms}",
                            level)
                return None
            final_atoms_for_clause.extend(_convert_interval_to_atoms(effective_range_interval, col_name))

    return AndOp(final_atoms_for_clause)

# =====================================================================
# Step 2 functions: Generate initial retrievals (shattering)
# =====================================================================

def generate_retrievals_recursive(original_simplified_dnf: OrOp,
                                  active_dnf_or_op: OrOp,
                                  index_columns: list,
                                  current_path_atoms: list,
                                  level) -> list:
    """
    Uses get_critical_points_for_column to find significant values from the
    predicates, and then generate_elementary_intervals to create basic,
    non-overlapping conditions for each dimension (index column).

    These elementary intervals, when combined across all dimensions, form the
    initial "shattered" retrieval paths from caller's original DNF representation.
    """

    def check_retrieval_satisfies_original_dnf(original_simplified_dnf: OrOp,
                                               retrieval_path_atoms: list):
        assert original_simplified_dnf is not None
        assert original_simplified_dnf.operands is not None

        path_map = {}
        for atom in retrieval_path_atoms:
            if atom.operator != Operator.IS_ANYTHING:
                path_map[atom.column] = atom
        for dnf_and_clause in original_simplified_dnf.operands:
            if not isinstance(dnf_and_clause, AndOp):
                continue
            if not dnf_and_clause.operands:
                return True
            clause_satisfied_by_path = True
            for dnf_atom in dnf_and_clause.operands:
                path_col_atom = path_map.get(dnf_atom.column)
                if not check_atom_compatibility(dnf_atom, path_col_atom):
                    clause_satisfied_by_path = False
                    break
            if clause_satisfied_by_path:
                return True
        return False

    # Base case: all columns processed -- keep path if it satisfies any DNF clause
    if not index_columns:
        if check_retrieval_satisfies_original_dnf(original_simplified_dnf,
                                                  current_path_atoms):
            return [deepcopy(current_path_atoms)]
        return []

    current_col = index_columns[0]
    remaining_cols = index_columns[1:]
    critical_points = get_critical_points_for_column(active_dnf_or_op.operands, current_col)
    elementary_intervals = generate_elementary_intervals(current_col, critical_points)

    all_retrievals = []
    for interval_atom in elementary_intervals:
        next_path = current_path_atoms[:]
        if interval_atom.operator != Operator.IS_ANYTHING:
            next_path.append(interval_atom)
        all_retrievals.extend(
            generate_retrievals_recursive(original_simplified_dnf, active_dnf_or_op,
                                          remaining_cols, next_path, level))
    return all_retrievals

def get_critical_points_for_column(dnf_and_clauses: list, column_name: str) -> list:
    """Collect all distinct boundary values for column_name from the DNF clauses."""
    points = set()
    for clause in dnf_and_clauses:
        for atom in clause.operands:
            if atom.column != column_name:
                continue
            if atom.operator == Operator.IN:
                points.update(atom.value)
            elif atom.operator == Operator.RANGE_EE:
                points.add(atom.value[0])
                points.add(atom.value[1])
            else:
                points.add(atom.value)
    return sorted(points)

def generate_elementary_intervals(column: str, critical_points: list) -> list[Atom]:
    """Create non-overlapping elementary intervals for a column from its critical points.

    For critical points [a, b], produces: <a, =a, (a,b), =b, >b
    """
    pts = sorted(critical_points)
    if not pts:
        return [Atom(column, Operator.IS_ANYTHING, None)]
    intervals = [Atom(column, Operator.LT, pts[0])]
    for i, p in enumerate(pts):
        intervals.append(Atom(column, Operator.EQ, p))
        if i + 1 < len(pts) and p < pts[i + 1]:
            intervals.append(Atom(column, Operator.RANGE_EE, (p, pts[i + 1])))
    intervals.append(Atom(column, Operator.GT, pts[-1]))
    return intervals

def check_atom_compatibility(atom_a: Atom, atom_b: Atom) -> bool:

    assert atom_a.column == atom_b.column

    op_a, val_a = atom_a.operator, atom_a.value
    op_b, val_b = atom_b.operator, atom_b.value

    def point_satisfies_atom(point, op, val):
        if op == Operator.IS_ANYTHING: return True
        if op == Operator.EQ:         return point == val
        if op == Operator.LT:         return point < val
        if op == Operator.LE:         return point <= val
        if op == Operator.GT:         return point > val
        if op == Operator.GE:         return point >= val
        if op == Operator.IN:         return point in val
        if op == Operator.RANGE_EE:   return val[0] < point < val[1]
        return False

    # --- Main logic of check_atom_compatibility ---

    # Specific handling for IN vs '=', '=' vs IN, and IN vs IN,
    # as _extract_effective_interval simplifies IN to a [min,max] range,
    # losing precision for discrete point checks needed here.
    # Also, point_satisfies_atom is well-suited for '=' vs other.
    if op_a == Operator.EQ:
        return point_satisfies_atom(val_a, op_b, val_b)
    if op_b == Operator.EQ:
        return point_satisfies_atom(val_b, op_a, val_a)

    # If one is 'IN' and the other is not '=' or 'IN' (e.g., <, >, RANGE_EE)
    # Check if any point in the IN list satisfies the other atom.
    if op_a == Operator.IN and op_b not in [Operator.EQ, Operator.IN]: # op_b would be like <, >, RANGE_EE
        assert(isinstance(val_a, (list, tuple)))
        return any(point_satisfies_atom(v, op_b, val_b) for v in val_a)
    if op_b == Operator.IN and op_a not in [Operator.EQ, Operator.IN]: # op_a would be like <, >, RANGE_EE
        assert(isinstance(val_b, (list, tuple)))
        return any(point_satisfies_atom(v, op_a, val_a) for v in val_b)

    # For all other combinations not covered above (e.g., range vs. range,
    # equality vs. range if not caught by point_satisfies_atom earlier, etc.):
    # Two atoms are compatible if their conjunction is not a contradiction.
    # This is true if _extract_effective_interval applied to both of them
    # results in a non-empty (non-None) interval.
    effective_combined_interval = _extract_effective_interval([atom_a, atom_b], atom_a.column)
    is_compatible = effective_combined_interval is not None

    if not is_compatible:
        print_debug(f"Contradiction detected by interval logic "
                    f"between {atom_a} and {atom_b}", 2)
    return is_compatible

# =====================================================================
# Step 3 helper functions: Interval arithmetic
# (also used by Steps 2 and 4)
# =====================================================================

def _extract_effective_interval(atoms_on_col: list[Atom], col_name: str) -> tuple | None:
    """
    Extracts a single effective interval (low, high, low_incl, high_incl)
    from a list of ANDed atoms for a single column.
    Returns None if atoms are contradictory.
    Uses None for infinity in bounds.
    """
    if not atoms_on_col:
        return (None, None, False, False) # Represents (-inf, +inf) effectively "IS_ANYTHING"

    # Helper to represent current interval being built
    # For internal logic, let's use float inf for easier comparison initially
    current_low, current_high = float('-inf'), float('inf')
    current_low_incl, current_high_incl = True, True # Start with fully inclusive universe

    for atom in atoms_on_col:
        op, val = atom.operator, atom.value
        next_low, next_high = float('-inf'), float('inf')
        next_low_incl, next_high_incl = True, True

        if op == Operator.EQ:
            next_low, next_high, next_low_incl, next_high_incl = val, val, True, True
        elif op == Operator.LT:
            next_high, next_high_incl = val, False
        elif op == Operator.LE:
            next_high, next_high_incl = val, True
        elif op == Operator.GT:
            next_low, next_low_incl = val, False
        elif op == Operator.GE:
            next_low, next_low_incl = val, True
        elif op == Operator.RANGE_EE:
            assert isinstance(val, tuple)
            assert len(val) == 2
            assert val[0] is not None
            assert val[1] is not None
            next_low, next_high, next_low_incl, next_high_incl = val[0], val[1], False, False
        elif op == Operator.IN:
            assert(isinstance(val, (list, tuple)) and val)
            # Filter Nones as they don't define points for a continuous interval in this context
            comparable_vals = [v_item for v_item in val if v_item is not None]
            if not comparable_vals:
                # IN (None) or IN () or IN (None, None) - cannot form a simple interval
                print_debug(f"IN list {val} for {col_name} "
                            f"has no comparable non-None values for interval extraction.", 2)
                return None

            min_v = min(comparable_vals)
            max_v = max(comparable_vals)
            # Represent IN as a closed interval [min, max]
            next_low, next_high, next_low_incl, next_high_incl = min_v, max_v, True, True
        else:
            assert op == Operator.IS_ANYTHING
            continue # No change to interval

        # Intersect current interval with this atom's interval
        # Update lower bound (take the more restrictive)
        if next_low != float('-inf'):
            if current_low == float('-inf') or next_low > current_low:
                current_low, current_low_incl = next_low, next_low_incl
            elif next_low == current_low:
                current_low_incl = current_low_incl and next_low_incl

        # Update upper bound (take the more restrictive)
        if next_high != float('inf'):
            if current_high == float('inf') or next_high < current_high:
                current_high, current_high_incl = next_high, next_high_incl
            elif next_high == current_high:
                current_high_incl = current_high_incl and next_high_incl

        # Check for contradictions (float values are only used for infinities)
        if not isinstance(current_low, float) and not isinstance(current_high, float):
            if current_low > current_high:
                return None
            if current_low == current_high and (not current_low_incl or not current_high_incl):
                return None

    # Convert float inf back to None for the canonical (None, val, incl, incl) representation
    final_low = None if current_low == float('-inf') else current_low
    final_high = None if current_high == float('inf') else current_high
    final_low_incl = current_low_incl if final_low is not None else False # Incl at inf is not meaningful here
    final_high_incl = current_high_incl if final_high is not None else False

    # Integer-specific: check for empty intervals like (4, 5, False, False) which
    # is valid for continuous types but empty for integers (no integer between 4 and 5)
    if (final_low is not None and final_high is not None and
            isinstance(final_low, int) and isinstance(final_high, int)):
        eff_low = final_low if final_low_incl else final_low + 1
        eff_high = final_high if final_high_incl else final_high - 1
        if eff_low > eff_high:
            return None

    return (final_low, final_high, final_low_incl, final_high_incl)


def _merge_interval_list(intervals: list[tuple]) -> list[tuple]:

    merged = []
    for current_interval in intervals:
        cl, ch, cli, chi = current_interval
        if not merged:
            merged.append(current_interval)
            continue

        ll, lh, lli, lhi = merged[-1] # Last merged interval

        # Check for mergeability:
        # current starts before or where last ended, considering inclusivity

        # ll: last_low, lh: last_high, lli: last_low_incl, lhi: last_high_incl
        # cl: current_low, ch: current_high, cli: current_low_incl, chi: current_high_incl

        # Condition for overlap or adjacency:
        # The start of the current interval (cl) must be less than or equal to the end of the last merged interval (lh).
        # If cl == lh, then at least one of the bounds must be inclusive for them to touch/overlap.
        # If lh is None (last interval goes to +inf), it can always merge with anything that starts.
        # If cl is None (current interval starts at -inf), it can merge if ll is also None or if cl <= lh.

        # Merge if C < B or (C == B and (B_inclusive or C_inclusive))
        if not (cl < lh or (cl == lh and (lhi or cli))):
            # Not mergeable
            merged.append(current_interval)
            continue

        # Merge: new end is max of ends. Inclusivity of new end depends on which interval provided it.
        new_high = lh
        new_high_incl = lhi
        if ch is None: # current goes to +inf
            new_high = None
            new_high_incl = False # Standard for None as inf
        elif lh is None: # last already goes to +inf, no change
            pass
        elif ch > lh:
            new_high = ch
            new_high_incl = chi
        elif ch == lh: # ends at same point
            new_high_incl = lhi or chi # If either was inclusive, merged is inclusive

        merged[-1] = (ll, new_high, lli, new_high_incl)

    return merged

def _convert_interval_to_atoms(interval: tuple, col_name: str) -> list[Atom]:
    low, high, low_incl, high_incl = interval
    atoms = []

    if low is None and high is None: # (-inf, +inf)
        return [] # Represents IS_ANYTHING, no specific atom needed

    if low is not None and high is not None:
        if low == high:
            return [Atom(col_name, Operator.EQ, low)]
        elif low > high:
            return None # Contradiction, 1=0

    if low is not None:
        atoms.append(Atom(col_name, Operator.GE if low_incl else Operator.GT, low))
    if high is not None:
        atoms.append(Atom(col_name, Operator.LE if high_incl else Operator.LT, high))

    assert atoms is not None

    return atoms

# =====================================================================
# Step 4 functions: Expand, sort, coalesce, and ordering conflict detection
# =====================================================================

def expand_and_sort_leading_constraints(dnf_predicate: OrOp,
                                        merged_retrievals: list[list[Atom]],
                                        index_cols_list: list[str],
                                        level) -> list[list[Atom]]:
    """Ensure keyspace-ordered scans by expanding leading IN/range constraints.

    For each merged retrieval path, expands IN lists and range constraints on
    leading index columns (those before the last constrained column) into
    elementary intervals.  This produces paths whose leading columns are point
    constraints, enabling proper index key space ordering across paths.
    """
    col_to_idx = {col: i for i, col in enumerate(index_cols_list)}

    def last_constrained_col_idx(path_atoms):
        return max((col_to_idx[a.column] for a in path_atoms
                    if a.column in col_to_idx and a.operator != Operator.IS_ANYTHING),
                   default=-1)

    all_expanded = []

    for path in merged_retrievals:
        paths = [path]

        for col_idx, col_name in enumerate(index_cols_list):
            new_paths = []
            for p in paths:
                if col_idx >= last_constrained_col_idx(p):
                    new_paths.append(p)
                    continue

                atom = next((a for a in p if a.column == col_name), None)
                is_anything = (atom is None or atom.operator == Operator.IS_ANYTHING)
                crit_pts = get_critical_points_for_column(dnf_predicate.operands, col_name)
                elem_intervals = generate_elementary_intervals(col_name, crit_pts)

                if is_anything:
                    # Shatter unconstrained column using elementary intervals
                    base = [a for a in p if a.column != col_name]
                    for ei in elem_intervals:
                        if ei.operator == Operator.IS_ANYTHING:
                            if len(elem_intervals) == 1:
                                new_paths.append(_sort_path(base))
                        else:
                            new_paths.append(_sort_path(base + [ei]))

                elif atom.operator == Operator.IN:
                    # Expand IN into individual EQ paths
                    for val in atom.value:
                        new_paths.append(_sort_path(
                            p + [Atom(col_name, Operator.EQ, val)]))

                else:
                    # Shatter range into elementary intervals
                    shattered = False
                    for ei in elem_intervals:
                        assert ei.operator != Operator.IS_ANYTHING
                        iv = _extract_effective_interval([atom, ei], col_name)
                        if iv and iv != (None, None, False, False):
                            iv_atoms = _convert_interval_to_atoms(iv, col_name)
                            if iv_atoms:
                                new_paths.append(_sort_path(p + iv_atoms))
                                shattered = True
                    if not shattered:
                        new_paths.append(p)

            paths = new_paths

        all_expanded.extend(paths)

    # Deduplicate and sort by index key space order
    seen = set()
    unique = []
    for p in all_expanded:
        key = tuple(p)
        if key not in seen:
            unique.append(p)
            seen.add(key)
    unique.sort(key=lambda p: get_path_sort_key(p, index_cols_list))
    return unique

def get_path_sort_key(path_atom_list: list[Atom], index_cols_list: list[str]) -> tuple:
    """
    Generates a sort key for a path based on the effective interval for each index column.
    The sort key for each column is ( (type_order_low, low_val), low_incl_sort,
                                     (type_order_high, high_val), high_incl_sort ).
    """
    key_components = []
    for col_name in index_cols_list:
        atoms_on_col = [a for a in path_atom_list if a.column == col_name]
        # _extract_effective_interval will now handle IN lists by creating a [min,max] interval
        interval = _extract_effective_interval(atoms_on_col, col_name)

        if interval is None:
            # This case should be rare if paths are well-formed (e.g. from simplify_disjunct)
            # It implies a contradiction or an unhandled complex state for the column.
            # For sorting, treat as a distinct, problematic case (e.g., sorts last).
            # Using (2,None) for type_order to make it sort after +inf or IS_ANYTHING.
            key_components.append( ( (2, None), True, (2, None), True ) )
            continue

        l, h, li, hi = interval

        sort_l_key_part = (-1, None) if l is None else (0, l)
        sort_h_key_part = (1, None) if h is None else (0, h)

        # For inclusivity sort: False (inclusive) should come before True (exclusive)
        # So, `not li` means `False` for inclusive (li=True), `True` for exclusive (li=False).
        # Python sorts False before True.
        key_components.append( (sort_l_key_part, not li, sort_h_key_part, not hi) )

    return tuple(key_components)

def detect_ordering_conflict(transformed_atoms: list[list[Atom]], index_cols_list: list[str]) -> str | None:
    """
    Detects if the given paths, when executed as UNION ALL subqueries, might produce
    an overall row order inconsistent with the original query's ORDER BY clause.

    Args:
        transformed_atoms: A list of paths, where each path is a list of Atom objects.
                           These paths are assumed to be sorted according to get_path_sort_key.
        index_cols_list: The list of index column names, defining the desired sort order.

    Returns:
        A string message describing the conflict if one is found, otherwise None.
    """
    if len(transformed_atoms) <= 1:
        return None # No conflict possible with 0 or 1 path

    ordering_conflict_message = None
    all_paths_computed_sort_keys = [get_path_sort_key(p, index_cols_list) for p in transformed_atoms]

    for k_path_idx in range(len(transformed_atoms) - 1):
        pk_sort_key = all_paths_computed_sort_keys[k_path_idx]
        pkplus1_sort_key = all_paths_computed_sort_keys[k_path_idx+1]
        print_debug(f"  Conflict Check: Comparing Path {k_path_idx} (key: {pk_sort_key}) "
                    f"with Path {k_path_idx+1} (key: {pkplus1_sort_key})", 2)
        first_diff_col_idx = -1
        for m_col_idx in range(len(index_cols_list)):
            if pk_sort_key[m_col_idx] != pkplus1_sort_key[m_col_idx]:
                first_diff_col_idx = m_col_idx
                print_debug(f"    First differing column index (m) is {first_diff_col_idx} "
                            f"('{index_cols_list[first_diff_col_idx]}')", 2)
                break
        if first_diff_col_idx == -1:
            print_debug(f"    Paths {k_path_idx} and {k_path_idx+1} "
                        "have identical sort keys. No conflict from this pair.", 2)
            continue
        for l_col_idx in range(first_diff_col_idx):
            col_l_name = index_cols_list[l_col_idx]
            common_sort_key_comp_l = pk_sort_key[l_col_idx]
            type_order_low  = common_sort_key_comp_l[0][0]
            val_low = common_sort_key_comp_l[0][1]
            low_incl = not common_sort_key_comp_l[1]
            type_order_high = common_sort_key_comp_l[2][0]
            val_high = common_sort_key_comp_l[2][1]
            high_incl = not common_sort_key_comp_l[3]
            is_point_constraint = (type_order_low == 0 and type_order_high == 0 and val_low == val_high and low_incl and high_incl)
            print_debug(f"      Col_l='{col_l_name}' (idx {l_col_idx}) "
                        f"common constraint: low={val_low} (incl {low_incl}), high={val_high} "
                        f"(incl {high_incl}). Is point: {is_point_constraint}", 2)
            if not is_point_constraint:
                col_m_name = index_cols_list[first_diff_col_idx]
                ordering_conflict_message = (
                    f"Index column '{col_l_name}' (pos {l_col_idx}) has an identical non-point constraint for adjacent paths {k_path_idx} and {k_path_idx+1}.\n"
                    f"The sort order between these paths is determined by a later column '{col_m_name}' (pos {first_diff_col_idx}),\n"
                    f"but the original query would sort by '{col_l_name}' first, potentially interleaving rows from these paths.\n"
                    f"  Path {k_path_idx} sort key: {pk_sort_key}\n  Path {k_path_idx+1} sort key: {pkplus1_sort_key}")
                return ordering_conflict_message # Return immediately on first conflict
    return None # No conflict found after checking all pairs

def retrieval_atoms_to_sql_where(retrieval_atoms: list, index_column_order: list) -> str:
    """Converts a flat list of Atom constraints into a SQL WHERE clause string.

    Atoms are ordered by index column position.  GE+LE pairs on the same
    column are rendered as BETWEEN for readability.
    """
    active_atoms = [a for a in retrieval_atoms if a.operator != Operator.IS_ANYTHING]
    if not active_atoms:
        return "1=1"

    parts = []
    used = set()

    for col_name in index_column_order:
        col_atoms = [a for a in active_atoms if a.column == col_name]
        if not col_atoms:
            continue

        # Combine GE + LE into BETWEEN when possible
        ge = next((a for a in col_atoms if a.operator == Operator.GE), None)
        le = next((a for a in col_atoms if a.operator == Operator.LE), None)
        if ge and le and ge.value <= le.value:
            parts.append(f"{col_name} BETWEEN {Atom.value_to_sql_literal(ge.value)} AND {Atom.value_to_sql_literal(le.value)}")
            used.update([id(ge), id(le)])

        for atom in col_atoms:
            if id(atom) not in used:
                parts.append(atom.to_sql())
                used.add(id(atom))

    return " AND ".join(parts)

def run_test_case(db_connection_params_local, test_info, test_number, log_file_tranformed=None):
    original_exec_time_ms = -1.0
    original_index_scan_nodes = -1 # Initialize here
    retrieval_mismatch_details = None # To store (expected, actual) if mismatch
    mdam_exec_time_ms = -1.0

    conn = None
    test_name = test_info['name'] # Moved here to be accessible in except blocks
    table_name = test_info["table_name"]
    index_cols_list = test_info["index_cols"]
    untransformed_predicate = test_info["predicate_constructor"]()
    expected_retrievals = test_info["expected_num_retrievals"]
    cols_to_select_str = ", ".join(index_cols_list)
    order_by_clause_str = ", ".join(index_cols_list)

    try:
        print(f"\n\n{'='*10} Running Test Case #{test_number}: '{test_name}' {'='*10}")

        # Perform "General OR transformation":
        transformed_atoms = generate_mdam_retrieval_clauses(untransformed_predicate,
                                                            index_cols_list)

        # Check expected number of retrievals against transformed atoms, too
        final_num_retrievals = len(transformed_atoms)
        if expected_retrievals is not None and final_num_retrievals != expected_retrievals:
            retrieval_mismatch_details = (expected_retrievals, final_num_retrievals)

        # Construct original query in SQL form:
        original_sql_predicate_where_part = untransformed_predicate.to_sql()
        original_full_sql = f"SELECT {cols_to_select_str} FROM {table_name} WHERE {original_sql_predicate_where_part} ORDER BY {order_by_clause_str};"

        # Construct SQL query using transformed/optimized atoms:
        transformed_sql_where = [retrieval_atoms_to_sql_where(r_path, index_cols_list) for r_path in transformed_atoms]
        if len(transformed_sql_where) == 0:
            mdam_sql_for_explain = f"SELECT {cols_to_select_str} FROM {table_name} WHERE 1=0;"
        else:
            union_all_parts = []
            for r, wc in enumerate(transformed_sql_where):
                retrieval_comment = f"/* Retrieval # {r+1} */"
                select_statement = f"(SELECT {cols_to_select_str} FROM {table_name} WHERE {wc})"
                union_all_parts.append(f"  {retrieval_comment}\n  {select_statement}\n")
            mdam_sql_retrievals = []
            for r, retrieval in enumerate(union_all_parts):
                if r > 0:
                    mdam_sql_retrievals.append("\nUNION ALL\n")
                mdam_sql_retrievals.append(retrieval)
            mdam_sql_body = "".join(mdam_sql_retrievals)
            mdam_sql_for_explain = f"SELECT * FROM (\n\n{mdam_sql_body.rstrip()}\n) AS mdam_subquery;"

        # Log the transformed SQL query if a log file is provided
        if log_file_tranformed:
            log_file_tranformed.write(f"-- Test Case: {test_info['name']}\n")
            log_file_tranformed.write(f"-- Transformed Query ({final_num_retrievals} retrievals):\n")
            formatted_log_sql = sqlparse.format(mdam_sql_for_explain,
                                                reindent=True,
                                                keyword_case='upper',
                                                indent_width=2,
                                                wrap_after=120)
            log_file_tranformed.write(formatted_log_sql + "\n\n")

        conn = psycopg2.connect(**db_connection_params_local)
        conn.autocommit = False

        # Always avoid parallel query, sequential scans (for both original and
        # transformed queries)
        with conn.cursor() as cur:
            cur.execute("set max_parallel_workers_per_gather=0")
            cur.execute("set enable_seqscan=off")

        # Execute EXPLAIN ANALYZE of original query:
        _, original_actual_rows, current_original_exec_time_ms, current_original_index_scan_nodes = \
                run_explain_analyze(conn, original_full_sql, "Original", test_name)

        original_exec_time_ms = current_original_exec_time_ms # Update the variable to be returned
        original_index_scan_nodes = current_original_index_scan_nodes # Update the variable to be returned
        print(f"Original SQL (EXPLAIN ANALYZE) - Actual Rows: {original_actual_rows}, Execution Time: {original_exec_time_ms:.3f} ms")

        with conn.cursor() as cur:
            cur.execute("set enable_bitmapscan=off") # Never used with transformed queries
        # Execute EXPLAIN ANALYZE of transformed/rewritten UNION ALL query:
        _, mdam_actual_rows, current_mdam_exec_time_ms, _ = \
                run_explain_analyze(conn, mdam_sql_for_explain, "Transformed", test_name)
        with conn.cursor() as cur:
            cur.execute("reset enable_bitmapscan")

        mdam_exec_time_ms = current_mdam_exec_time_ms # Update the variable to be returned
        print(f"Transformed Query (EXPLAIN ANALYZE) - Actual Rows: {mdam_actual_rows}, Execution Time: {mdam_exec_time_ms:.3f} ms")
        if original_actual_rows >= 0 and mdam_actual_rows >= 0:
            if original_actual_rows == mdam_actual_rows:
                print(f"\nINFO: EXPLAIN ANALYZE row counts match for '{test_info['name']}' ({original_actual_rows} rows).")
            else:
                print(f"\nWARNING: EXPLAIN ANALYZE row counts DO NOT MATCH for '{test_info['name']}'. Original: {original_actual_rows}, MDAM: {mdam_actual_rows}.")
        else:
            print(f"\nINFO: Could not reliably compare EXPLAIN ANALYZE row counts for '{test_info['name']}' (one or both were -1).")

        # Check if the transformed retrievals can preserve index key space order
        ordering_conflict_message = detect_ordering_conflict(transformed_atoms,
                                                             index_cols_list)
        expects_ordering_failure = test_info.get("expected_ordering_failure", False)

        if ordering_conflict_message:
            print(f"\n{'~'*60}")
            print(f"Ordering conflict detected for '{test_name}':")
            print(ordering_conflict_message)
            print(f"{'~'*60}")

        print("\n--- Verifying full result set content and order ---")

        # Execute both the original and transformed queries
        original_query_results = execute_and_fetch_all(conn, original_full_sql, "Ordered Original Query")
        with conn.cursor() as cur:
            cur.execute("set enable_bitmapscan=off")
        mdam_query_results = execute_and_fetch_all(conn, mdam_sql_for_explain, "Transformed Query")
        with conn.cursor() as cur:
            cur.execute("reset enable_bitmapscan")

        if original_query_results is None or mdam_query_results is None:
            raise ValueError(f"SQL execution error fetching results for '{test_name}'.")

        if original_query_results == mdam_query_results:
            print(f"\n  Result sets match (content, order, count: {len(original_query_results)} rows).")
            if expects_ordering_failure:
                print(f"\n  NOTE: Test was marked as expecting an ordering failure, but results matched anyway!")
            print(f"--- Test Case '{test_name}' PASSED ---")
            return "PASSED", test_name, original_exec_time_ms, mdam_exec_time_ms, final_num_retrievals, retrieval_mismatch_details, original_index_scan_nodes

        # Results don't match exactly -- figure out if it's just ordering
        if sorted(original_query_results) == sorted(mdam_query_results):
            # Same rows, different order
            if expects_ordering_failure and ordering_conflict_message:
                print(f"\n  Row content matches ({len(original_query_results)} rows), but order differs.")
                print(f"  This is expected: the transformation cannot preserve index key")
                print(f"  space ordering for this predicate structure.")
                print(f"--- Test Case '{test_name}' EXPECTED_FAILURE (ordering) ---")
                return "EXPECTED_FAILURE", test_name, original_exec_time_ms, mdam_exec_time_ms, final_num_retrievals, retrieval_mismatch_details, original_index_scan_nodes
            else:
                raise ValueError(
                    f"Result set ordering mismatch for '{test_name}' "
                    f"({len(original_query_results)} rows, same content, different order).")
        else:
            # Actual content mismatch
            if len(original_query_results) != len(mdam_query_results):
                detail = f"Row count mismatch: Original={len(original_query_results)}, MDAM={len(mdam_query_results)}."
            else:
                detail = "Row counts match, but content differs (not just ordering)."
            raise ValueError(f"Result set mismatch for '{test_name}': {detail}")

    except psycopg2.Error as e:
        print(f"Database error for test '{test_name}': {e}")
        return "FAILED", test_name, \
            original_exec_time_ms, mdam_exec_time_ms, \
                final_num_retrievals, retrieval_mismatch_details, original_index_scan_nodes
    except ValueError as e:
        print(f"\n  FAILED: {e}")
        return "FAILED", \
            test_name, \
            original_exec_time_ms, mdam_exec_time_ms, \
            final_num_retrievals, retrieval_mismatch_details, original_index_scan_nodes
    finally:
        if conn:
            conn.rollback() # Ensure rollback on any exit path from try block
            conn.close()
        print(f"--- Test Case '{test_name}' Processing Complete ---")

def run_explain_analyze(conn, sql_query_to_explain, query_type: str, test_case_name: str):

    def parse_explain_analyze_output(lines):
        actual_rows = -1
        execution_time_ms = -1.0
        index_scan_count = 0

        for line in lines:
            m = re.search(r"\(actual[^)]*rows=(\d+)[^)]*\)", line)
            if m and actual_rows == -1:
                actual_rows = int(m.group(1))
            m = re.search(r"Execution Time: (\d+\.\d+) ms", line)
            if m:
                execution_time_ms = float(m.group(1))
            # Count index scan nodes (Bitmap Index Scan, Index Only Scan, Index Scan)
            if re.search(r"(Bitmap Index Scan|Index Only Scan|Index Scan)\b", line):
                index_scan_count += 1

        return actual_rows, execution_time_ms, index_scan_count if lines else -1

    plan_lines = []

    formatted_sql_display = sqlparse.format(sql_query_to_explain,
                                            reindent=True,
                                            keyword_case='upper',
                                            indent_width=2,
                                            wrap_after=120)
    full_query_display_name = f"{query_type} \"{test_case_name}\" Query"
    print(f"\n-- {full_query_display_name}: \n{formatted_sql_display}\n")

    # During debugging it's sometimes useful to speed things up by not actually
    # executing queries (transformed query texts give a clear enough picture on their own)
    if DEBUG_DONT_QUERY:
        return None, -1, -1.0, -1 # plan_lines, rows, time, index_scan_nodes

    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (ANALYZE, VERBOSE, BUFFERS) {sql_query_to_explain}")
            for record in cur:
                plan_lines.append(record[0])
            actual_rows, execution_time_ms, index_scan_nodes_count = parse_explain_analyze_output(plan_lines)

            print(f"EXPLAIN ANALYZE Output ({full_query_display_name}):")
            for line in plan_lines:
                print(f"  {line}")
    except psycopg2.Error as e:
        print(f"SQL Error during EXPLAIN ANALYZE ({full_query_display_name}): {e}\nQuery was:\n{sql_query_to_explain}")
        return None, -1, -1.0, -1 # plan_lines, rows, time, index_scan_nodes
    return plan_lines, actual_rows, execution_time_ms, index_scan_nodes_count

def execute_and_fetch_all(conn, sql_query, query_display_name):

    rows = []

    if DEBUG_DONT_QUERY:
        return rows

    try:
        with conn.cursor() as cur:
            cur.execute(sql_query)
            rows = cur.fetchall()
        print(f"Fetched {len(rows)} rows for {query_display_name} (for full result set verification).")
        return rows
    except psycopg2.Error as e:
        formatted_sql_display = sqlparse.format(sql_query, reindent=True,
                                                keyword_case='upper',
                                                indent_width=2,
                                                compact=False, wrap_after=80)
        print(f"SQL Error during full execution of {query_display_name}: {e}\nQuery was:\n{formatted_sql_display}")
        return None # Indicate error

if __name__ == "__main__":

    # General OR Optimization example query's predicate:
    #
    # WHERE
    #   ((item_class = 10 AND sdate >= '1995-06-04' AND sdate <= '1995-06-25') OR
    #    (dept IN (2, 4, 5)))
    #   AND
    #   ((dept = 4 AND item_class = 5) OR
    #    (item_class IN (5, 10) AND (sdate = '1995-06-04' OR
    #                                dept = 2)))
    #
    # "Table 3" retrievals from MDAM paper for said query:
    #
    # Retrieval #   Dept        Date                                Item_Class
    # --------------------------------------------------------------------------
    # 1             < 2          =  '1995-06-04'                    = 10
    # 2             = 2          <  '1995-06-04'                    = 5 or = 10
    # 3             = 2          >= '1995-06-04' & <= '1995-06-25'  = 10
    # 4             = 2          >  '1995-06-25'                    = 5 or = 10
    # 5             > 2 & < 4    =  '1995-06-04'                    = 10
    # 6             = 4          <  '1995-06-04'                    = 5
    # 7             = 4          =  '1995-06-04'                    = 5 or = 10
    # 8             = 4          >  '1995-06-04'                    = 5
    # 9             = 5          =  '1995-06-04'                    = 5
    # 10            = 5          =  '1995-06-04'                    = 10
    # 11            > 5          =  '1995-06-04'                    = 10
    #
    # NOTE: I think that the paper is wrong!  The only "dept=2" retrieval _actually_
    # required seems to be:
    #
    # 2             = 2          = IS_ANYTHING                      = 5 or = 10
    #
    # The paper presents incorrect (not just inefficient) accesses here.  To at
    # least be correct, retrieval # 3 would have to be:
    #
    # 3             = 2          >= '1995-06-04' & <= '1995-06-25'  = 5 or = 10
    #
    # At which point it's safe to merge together all "dept=2" retrievals --
    # which is what we actually do.  Now there are only 9 retrievals.
    #
    # The paper also misses a trick here, for whatever reason:
    #
    # 9             = 5          =  '1995-06-04'                    = 5
    # 10            = 5          =  '1995-06-04'                    = 10
    # 11            > 5          =  '1995-06-04'                    = 10
    #
    # These two can be merged together, so we're able to do this instead:
    #
    # 7             = 5          =  '1995-06-04'                    = 5 or = 10
    # 8             > 5          =  '1995-06-04'                    = 10
    #
    # That's how we're able to do 8 retrievals for this particular query, instead
    # of the 11 from the paper.
    def test_construct_mdam_paper_example_predicate():
        p1_ic10 = Atom("item_class", Operator.EQ, 10)
        p1_sdate_ge = Atom("sdate", Operator.GE, date(1995, 6, 4))
        p1_sdate_le = Atom("sdate", Operator.LE, date(1995, 6, 25))
        p2_dept_in = Atom("dept", Operator.IN, [2,4,5])
        clause_p1 = AndOp([p1_ic10, p1_sdate_ge, p1_sdate_le])
        clause_p2 = AndOp([p2_dept_in])
        term1 = OrOp([clause_p1, clause_p2])
        p3_dept4 = Atom("dept", Operator.EQ, 4)
        p3_ic5 = Atom("item_class", Operator.EQ, 5)
        clause_p3 = AndOp([p3_dept4, p3_ic5])
        p4_ic_in = Atom("item_class", Operator.IN, [5,10])
        p4_sdate_eq = Atom("sdate", Operator.EQ, date(1995, 6, 4))
        p4_dept2 = Atom("dept", Operator.EQ, 2)
        p4_inner_or = OrOp([ AndOp([p4_sdate_eq]), AndOp([p4_dept2]) ])
        complex_and_in_term2 = AndOp([p4_ic_in, p4_inner_or])
        term2 = OrOp([clause_p3, complex_and_in_term2])
        return AndOp([term1, term2])

    def test_construct_sql_doc_predicate():
        # Corresponds to:
        # ((item_class = 10 AND sdate BETWEEN '1995-02-05' AND '1995-02-10') OR dept IN (2, 4, 5)) AND
        # ((dept = 4 AND item_class = 5) OR (item_class IN (5,10) AND (sdate = '1995-02-05' OR dept = 2)))
        p1_ic10 = Atom("item_class", Operator.EQ, 10)
        p1_sdate_ge = Atom("sdate", Operator.GE, date(1995, 6, 4))
        p1_sdate_le = Atom("sdate", Operator.LE, date(1995, 6, 25))
        p2_dept_in = Atom("dept", Operator.IN, [2,4,5])
        clause_p1 = AndOp([p1_ic10, p1_sdate_ge, p1_sdate_le])
        clause_p2 = AndOp([p2_dept_in])
        term1 = OrOp([clause_p1, clause_p2])
        p3_dept4 = Atom("dept", Operator.EQ, 4)
        p3_ic5 = Atom("item_class", Operator.EQ, 5)
        clause_p3 = AndOp([p3_dept4, p3_ic5])
        p4_ic_in = Atom("item_class", Operator.IN, [5,10])
        p4_sdate_eq = Atom("sdate", Operator.EQ, date(1995, 2, 5))
        p4_dept2 = Atom("dept", Operator.EQ, 2)
        p4_inner_or = OrOp([ AndOp([p4_sdate_eq]), AndOp([p4_dept2]) ])
        complex_and_in_term2 = AndOp([p4_ic_in, p4_inner_or])
        term2 = OrOp([clause_p3, complex_and_in_term2])
        return AndOp([term1, term2])

    def test_construct_complex_and_or_predicate():
        term_a_sub_and1 = AndOp([Atom("store", Operator.GT, 10), Atom("item_class", Operator.EQ, 5)])
        term_a_sub_and2 = AndOp([Atom("dept", Operator.EQ, 1), Atom("sdate", Operator.LT, date(1995, 3, 1))])
        complex_term_a = OrOp([term_a_sub_and1, term_a_sub_and2])
        term_b_sub_or_sub_and1 = AndOp([Atom("item_class", Operator.EQ, 5), Atom("dept", Operator.EQ, 1)])
        term_b_sub_or_sub_and2 = Atom("sdate", Operator.EQ, date(1995, 2, 15))
        term_b_sub_or = OrOp([term_b_sub_or_sub_and1, term_b_sub_or_sub_and2])

        complex_term_b = AndOp([Atom("store", Operator.LT, 100), term_b_sub_or])

        # Top level: Term A AND Term B
        return AndOp([complex_term_a, complex_term_b])

    def test_construct_really_complex_and_or_predicate():

        term_b_sub_or_sub_and1 = AndOp([Atom("dept", Operator.EQ, 1), Atom("item_class", Operator.IN, [5, 7]) ])
        term_b_sub_or = OrOp([term_b_sub_or_sub_and1, Atom("sdate", Operator.EQ, date(1995, 2, 15))])

        complex_term_b = AndOp([Atom("store", Operator.LT, 100), term_b_sub_or])

        # Top level: Term A AND Term B
        return AndOp([Atom("store", Operator.GT, 10), complex_term_b])

    def test_construct_super_complex_and_or_predicate():
        complex_term_a = AndOp([Atom("dept", Operator.EQ, 1), Atom("sdate", Operator.LT, date(1995, 1, 3))])

        term_b_sub_or_sub_and1 = AndOp([Atom("dept", Operator.EQ, 1), Atom("item_class", Operator.IN, [5, 7])])
        term_b_sub_or_sub_and2 = Atom("sdate", Operator.EQ, date(1995, 1, 2))
        term_b_sub_or = OrOp([term_b_sub_or_sub_and1, term_b_sub_or_sub_and2])

        complex_term_b = AndOp([Atom("store", Operator.LT, 10), term_b_sub_or])

        return AndOp([complex_term_a, complex_term_b])

    def test_construct_hairy_and_or_predicate():
        # Term A: (store > 10 AND item_class = 5) OR (dept IN (1,2) AND sdate < '1995-03-01')
        term_a_sub_and1 = AndOp([Atom("store", Operator.GT, 10), Atom("item_class", Operator.EQ, 5)])
        term_a_sub_and2 = AndOp([Atom("dept", Operator.IN, [1, 2]), Atom("sdate", Operator.LT, date(1995, 3, 1))])
        complex_term_a = OrOp([term_a_sub_and1, term_a_sub_and2])

        # Term B: store < 100 AND ((item_class IN (5,7) AND dept = 1) OR (sdate = '1995-02-15' AND store > 20))
        term_b_sub_or_sub_and1 = AndOp([Atom("item_class", Operator.IN, [5, 7]), Atom("dept", Operator.EQ, 1)])
        term_b_sub_or_sub_and2 = AndOp([Atom("sdate", Operator.EQ, date(1995, 2, 15)), Atom("store", Operator.GT, 20)])
        term_b_sub_or = OrOp([term_b_sub_or_sub_and1, term_b_sub_or_sub_and2])

        complex_term_b = AndOp([Atom("store", Operator.LT, 100), term_b_sub_or])

        # Top level: Term A AND Term B
        return AndOp([complex_term_a, complex_term_b])

    def test_construct_expansion_logic_shattering_predicate():
        sdate_d1 = date(1995, 1, 1)
        sdate_d2 = date(1995, 2, 1)
        sdate_d3 = date(1995, 3, 1)

        # Condition_A part 1: (dept = 10 OR dept = 20)
        dept_options = OrOp([
            AndOp([Atom("dept", Operator.EQ, 10)]),
            AndOp([Atom("dept", Operator.EQ, 20)])
        ])

        # Condition_A part 2: item_class = 5
        ic5 = Atom("item_class", Operator.EQ, 5)

        # Condition_A part 3: (sdate > '1995-01-01' AND sdate < '1995-03-01')
        sdate_range_cond = AndOp([
            Atom("sdate", Operator.GT, sdate_d1),
            Atom("sdate", Operator.LT, sdate_d3)
        ])

        # Condition_A: (dept IN (10,20)) AND item_class = 5 AND (sdate > D1 AND sdate < D3)
        condition_a = AndOp([dept_options, ic5, sdate_range_cond])

        # Condition_B: sdate = '1995-02-01' AND item_class = 5
        condition_b = AndOp([
            Atom("sdate", Operator.EQ, sdate_d2),
            ic5 # re-use ic5
        ])

        # (Condition_A OR Condition_B)
        or_group_ab = OrOp([condition_a, condition_b])

        # Condition_C: (store = 100 OR store = 200)
        store_options = OrOp([AndOp([Atom("store", Operator.EQ, 100)]), AndOp([Atom("store", Operator.EQ, 200)])])
        # Final Predicate: (Condition_A OR Condition_B) AND Condition_C
        final_predicate = AndOp([or_group_ab, store_options])
        return final_predicate

    def test_construct_maximal_shatter_then_coalesce():
        # Tests if expand_and_sort_leading_constraints shatters leading INs,
        # and then coalesce_adjacent_path_ranges merges them back.
        # dept IN (1,2) -> 2 paths
        # sdate IN (D1,D2) -> 2 sub-paths each. Total 2*2 = 4 elementary paths.
        # Coalesce should bring it back to 1 path.
        d1 = date(1995, 3, 1)
        d2 = date(1995, 3, 2)
        return AndOp([
            Atom("dept", Operator.IN, [1, 2]),
            Atom("sdate", Operator.IN, [d1, d2]),
            Atom("item_class", Operator.EQ, 5),
            Atom("store", Operator.GT, 100)
        ])

    def test_construct_is_anything_propagation():
        # (dept = 10 AND item_class > 5 AND store = 100) OR
        # (dept = 10 AND item_class <= 5 AND store = 100)
        # This should simplify to (dept = 10 AND store = 100), with item_class becoming IS_ANYTHING.
        # 1. merge_retrievals: item_class becomes IS_ANYTHING. Path: [dept=10, store=100]
        # 2. expand_and_sort: item_class (IS_ANYTHING) is shattered because store (later col) is constrained.
        #    Gives 3 paths based on item_class <5, =5, >5.
        # 3. coalesce_adjacent_path_ranges: merges these 3 paths back on item_class to IS_ANYTHING.
        # Final expected retrievals: 1
        return OrOp([
            AndOp([Atom("dept", Operator.EQ, 10), Atom("item_class", Operator.GT, 5), Atom("store", Operator.EQ, 100)]),
            AndOp([Atom("dept", Operator.EQ, 10), Atom("item_class", Operator.LE, 5), Atom("store", Operator.EQ, 100)])
        ])

    # NOTE: Only Possible to make this generate retrievals in DNF, but not while
    # preserving index key space order/the appearance of one continual scan.
    # This will raise an OrderingConflictError because it is fundamentally
    # impossible to make it work within our current constraints.
    #
    # See also: later tests that are designed to be as similar as possible to
    # this one, without triggering an OrderingConflictError.
    #
    # If allowed to run, this test fails to return rows in index key space order,
    # so the test fails (but it does at least return the correct rows in incorrect
    # order).
    #
    # There seems to be no possible way to make this work within the confines
    # of any approach where individual nbtree quals are always in CNF (CNF once
    # we ignore any ScalarArrayOps, which are technically a form of "inner OR").
    #
    # Theoretically, we could deal with this by making it generate many
    # distinct = conditions for (say) "dept".  But that's wildly impractical;
    # there's no way of knowing how many total "dept" values there might be.
    #
    # Alternatively, we could deal with this by tolerating the rows being out
    # of order/not generating path keys for the append node.  That seems bad.
    #
    # Alternatively, we could deal with this by using filter quals for
    # "item_class <= 2 AND store > 240".  This seems rather unappealing, for
    # all the usual reasons (filter quals lead to excessive heap accesses).
    #
    # Alternatively, we could deal with this by inventing new nbtree concepts
    # that invent other new kinds of "inner OR" constructs.  This has downsides
    # that are somewhat similar to the filter qual idea.  Mostly I don't like
    # the idea because it just doesn't seem likely to be worth it.
    #
    # The most plausible solution is that we just don't do MDAM transformation
    # at all when we encounter this problem.
    def test_construct_ordering_conflict():
        # Triggers OrderingConflictError:
        #
        # SELECT dept, sdate, item_class, store
        # FROM sales_mdam_paper
        # WHERE dept > 10
        # AND sdate = '1995-03-01'
        # AND (item_class = 5
        #      OR store = 50)
        # ORDER BY dept, sdate, item_class, store;
        #
        # DNF: (dept > 10 AND sdate = '1995-03-01' AND item_class = 5) OR
        #      (dept > 10 AND sdate = '1995-03-01' AND store = 50)
        #
        # 'dept' is a common non-point constraint.
        # 'sdate' is a common point constraint.
        # The OR splits on item_class vs store.
        common_prefix = AndOp([Atom("dept", Operator.GT, 10), Atom("sdate", Operator.EQ, date(1995,3,1))])
        or_part = OrOp([Atom("item_class", Operator.EQ, 5), Atom("store", Operator.EQ, 50)])
        return AndOp([common_prefix, or_part])

    # This is similar to the above (it also fails due to an OrderingConflict),
    # but the split point is on "sdate", not on "dept"
    def test_construct_ordering_conflict_later_col():
        # DNF: (dept = 10 AND sdate > '1995-02-01' AND item_class = 5) OR
        #      (dept = 10 AND sdate > '1995-02-01' AND store = 50)
        # 'dept' is a common point constraint.
        # 'sdate' is a common non-point constraint.
        # The OR splits on item_class vs store. Conflict should be on 'sdate'.
        common_prefix = AndOp([Atom("dept", Operator.EQ, 10), Atom("sdate", Operator.GT, date(1995,2,1))])
        or_part = OrOp([Atom("item_class", Operator.EQ, 5), Atom("store", Operator.EQ, 50)])
        return AndOp([common_prefix, or_part])

    # Here's a very similar expression/qual that _can_ generate retrievals in
    # DNF, __while preserving index key space order__
    def test_construct_no_ordering_conflict():
        # _Won't_ trigger OrderingConflictError.
        #
        # SELECT dept, sdate, item_class, store
        # FROM sales_mdam_paper
        # WHERE dept > 10
        # AND sdate = '1995-03-01'
        # AND (store = 5         -- unlike test_construct_ordering_conflict
        #      OR store = 50)
        # ORDER BY dept, sdate, item_class, store;
        #
        # DNF: (dept > 10 AND sdate = '1995-03-01' AND store IN (5, 50))
        #
        # We made the multiple-column OR condition from
        # test_construct_ordering_conflict use the same column twice.
        # We use (store = 5 OR store = 50) for this test variant (no item_class use).
        common_prefix = AndOp([Atom("dept", Operator.GT, 10), Atom("sdate", Operator.EQ, date(1995,3,1))])
        or_part = OrOp([Atom("store", Operator.EQ, 5), Atom("store", Operator.EQ, 50)])
        return AndOp([common_prefix, or_part])

    # Here's another very similar expression/qual that _can_ generate
    # retrievals in DNF __while preserving index key space order__
    def test_construct_still_no_ordering_conflict():
        # This structure _still_ won't trigger OrderingConflictError.
        #
        # SELECT dept, sdate, item_class, store
        # FROM sales_mdam_paper
        # WHERE dept = 10        -- unlike test_construct_ordering_conflict
        # AND sdate = '1995-03-01'
        # AND (item_class = 5
        #      OR store = 50)
        # ORDER BY dept, sdate, item_class, store;
        #
        # DNF: (dept = 10 AND sdate = '1995-03-01' AND item_class < 5 AND store = 50) OR
        #      (dept = 10 AND sdate = '1995-03-01' AND item_class = 5) OR
        #      (dept = 10 AND sdate = '1995-03-01' AND item_class > 5 AND store = 50)
        #
        # We made "dept > 10" into "dept = 10", but kept the multiple-column OR condition
        # from test_construct_ordering_conflict (it's the combination that's unsafe).
        common_prefix = AndOp([Atom("dept", Operator.EQ, 10), Atom("sdate", Operator.EQ, date(1995,3,1))])
        or_part = OrOp([Atom("item_class", Operator.EQ, 5), Atom("store", Operator.EQ, 50)])
        return AndOp([common_prefix, or_part])

    def test_construct_merge_retrievals_gap_splitting_predicate():
        sdate_d1 = date(1995, 6, 4)
        sdate_d3 = date(1995, 6, 6) # Note: D3, so there's a D2 (June 5th) in between

        # Common base for all clauses
        common_base_list = [
            Atom("dept", Operator.EQ, 1),
            Atom("item_class", Operator.EQ, 10)
        ]

        # Clause 1: sdate = D1
        clause1 = AndOp(deepcopy(common_base_list) + [Atom("sdate", Operator.EQ, sdate_d1)])

        # Clause 2: sdate = D3
        clause2 = AndOp(deepcopy(common_base_list) + [Atom("sdate", Operator.EQ, sdate_d3)])

        # Clause 3: sdate > D1 AND sdate < D3
        clause3 = AndOp(deepcopy(common_base_list) + [
            Atom("sdate", Operator.GT, sdate_d1),
            Atom("sdate", Operator.LT, sdate_d3)
        ])
        return OrOp([clause1, clause2, clause3])

    TABLE_NAME = "sales_mdam_paper"
    test_cases = [
        {
             "name": "MDAM Paper General OR Example",
             "predicate_constructor": test_construct_mdam_paper_example_predicate,
             "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
             "expected_num_retrievals": 8
        },
        {
             "name": "SQL docs predicate",
             "predicate_constructor": test_construct_sql_doc_predicate,
             "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
             "expected_num_retrievals": 5
        },
        {
            # This is so simple that MySQL can do it.
            # See:
            # https://dev.mysql.com/doc/refman/8.4/en/range-optimization.html,
            # particularly "Range Access Method for Multiple-Part Indexes"
            "name": "Simple MySQL style disjunction",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.EQ, 1), Atom("sdate", Operator.LT, date(1995, 1, 10)), Atom("item_class", Operator.EQ, 1), Atom("store", Operator.EQ, 50) ]),
                AndOp([Atom("dept", Operator.GT, 98), Atom("sdate", Operator.EQ, date(1995, 6, 7)), Atom("item_class", Operator.EQ, 28), Atom("store", Operator.EQ, 3) ]),
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 2
        },
        {
            # Intuitively, feels like Postgres 18/HEAD should already be able
            # to do the trick that we can do here, since it seems like a simple
            # "OR transformation of constants" problem.
            #
            # Here's part of the current untransformed plan for this:
            #
            # ->  BitmapOr  (cost=12.96..12.96 rows=4 width=0) (actual time=0.028..0.028 rows=0.00 loops=1)
            #    Buffers: shared hit=20
            #    ->  Bitmap Index Scan on mdam_idx  (cost=0.00..2.59 rows=1 width=0) (actual time=0.013..0.013 rows=1.00 loops=1)
            #          Index Cond: ((sales_mdam_paper.dept = 9) AND (sales_mdam_paper.sdate = '1995-06-15'::date) AND (sales_mdam_paper.item_class = 1) AND (sales_mdam_paper.store = 50))
            #          Index Searches: 1
            #          Buffers: shared hit=4
            #    ->  Bitmap Index Scan on mdam_idx  (cost=0.00..2.59 rows=1 width=0) (actual time=0.005..0.005 rows=1.00 loops=1)
            #          Index Cond: ((sales_mdam_paper.dept = 9) AND (sales_mdam_paper.sdate = '1995-06-15'::date) AND (sales_mdam_paper.item_class = 1) AND (sales_mdam_paper.store = 55))
            #          Index Searches: 1
            #          Buffers: shared hit=4
            #    ->  Bitmap Index Scan on mdam_idx  (cost=0.00..2.59 rows=1 width=0) (actual time=0.003..0.003 rows=1.00 loops=1)
            #          Index Cond: ((sales_mdam_paper.dept = 9) AND (sales_mdam_paper.sdate = '1995-06-15'::date) AND (sales_mdam_paper.item_class = 2) AND (sales_mdam_paper.store = 50))
            #          Index Searches: 1
            #          Buffers: shared hit=4
            #    ->  Bitmap Index Scan on mdam_idx  (cost=0.00..2.59 rows=1 width=0) (actual time=0.001..0.001 rows=1.00 loops=1)
            #          Index Cond: ((sales_mdam_paper.dept = 9) AND (sales_mdam_paper.sdate = '1995-06-15'::date) AND (sales_mdam_paper.item_class = 2) AND (sales_mdam_paper.store = 55))
            #          Index Searches: 1
            #          Buffers: shared hit=4
            #    ->  Bitmap Index Scan on mdam_idx  (cost=0.00..2.59 rows=1 width=0) (actual time=0.004..0.004 rows=1.00 loops=1)
            #          Index Cond: ((sales_mdam_paper.dept = 10) AND (sales_mdam_paper.sdate = '1995-06-15'::date) AND (sales_mdam_paper.item_class = 1) AND (sales_mdam_paper.store = 50))
            #          Index Searches: 1
            #          Buffers: shared hit=4
            #
            # Why can't we make the first 4 index scans into 2 index scans,
            # that can use "store IN (50, 55)"? Seems like it wouldn't be too
            # hard to do that, independent of all this MDAM stuff.
            "name": "BitmapOr SAOP conversion limitation",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.EQ, 9), Atom("sdate", Operator.EQ, date(1995, 6, 15)), Atom("item_class", Operator.EQ, 1), Atom("store", Operator.EQ, 50) ]),
                AndOp([Atom("dept", Operator.EQ, 9), Atom("sdate", Operator.EQ, date(1995, 6, 15)), Atom("item_class", Operator.EQ, 1), Atom("store", Operator.EQ, 55) ]),
                AndOp([Atom("dept", Operator.EQ, 9), Atom("sdate", Operator.EQ, date(1995, 6, 15)), Atom("item_class", Operator.EQ, 2), Atom("store", Operator.EQ, 50) ]),
                AndOp([Atom("dept", Operator.EQ, 9), Atom("sdate", Operator.EQ, date(1995, 6, 15)), Atom("item_class", Operator.EQ, 2), Atom("store", Operator.EQ, 55) ]),
                AndOp([Atom("dept", Operator.EQ, 10), Atom("sdate", Operator.EQ, date(1995, 6, 15)), Atom("item_class", Operator.EQ, 1), Atom("store", Operator.EQ, 50) ]),
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 2
        },
        {
            # Postgres 18 already does things perfectly here, but we do no
            # worse (we just waste cycles to produce the same query)
            "name": "Multiple ORs on different columns",
            "predicate_constructor": lambda: AndOp([
                OrOp([AndOp([Atom("dept", Operator.EQ, 10)]), AndOp([Atom("dept", Operator.EQ, 20)])]),
                OrOp([AndOp([Atom("sdate", Operator.EQ, date(1995,1,1))]), AndOp([Atom("sdate", Operator.EQ, date(1995,1,15))])]),
                OrOp([AndOp([Atom("item_class", Operator.EQ, 1)]), AndOp([Atom("item_class", Operator.EQ, 2)])])
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "IS_ANYTHING Shattering Scope",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.EQ, 10), Atom("item_class", Operator.EQ, 5)]),
                AndOp([Atom("dept", Operator.EQ, 10), Atom("sdate", Operator.EQ, date(1995, 2, 1)), Atom("item_class", Operator.EQ, 7)]),
                AndOp([Atom("dept", Operator.EQ, 10), Atom("sdate", Operator.EQ, date(1995, 3, 1)), Atom("item_class", Operator.EQ, 7)]),
                AndOp([Atom("dept", Operator.EQ, 20), Atom("item_class", Operator.EQ, 1)])
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 6 # Based on current code's behavior
        },
        {
            "name": "Mixed IN and Range (Contradiction)",
            "predicate_constructor": lambda: AndOp([
                Atom("dept", Operator.IN, [10, 20]),
                Atom("dept", Operator.GT, 25)
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 0
        },
        {
            "name": "Highly Combinatorial ORs",
            "predicate_constructor": lambda: AndOp([
                OrOp([Atom("dept", Operator.EQ, 1), Atom("dept", Operator.EQ, 2)]),
                OrOp([Atom("sdate", Operator.EQ, date(1995,1,1)), Atom("sdate", Operator.EQ, date(1995,1,15))]),
                OrOp([Atom("item_class", Operator.EQ, 1), Atom("item_class", Operator.EQ, 2), Atom("item_class", Operator.EQ, 3)]),
                OrOp([Atom("store", Operator.EQ, 100), Atom("store", Operator.EQ, 200)])
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "Expansion Suppression",
            "predicate_constructor": lambda: AndOp([
                Atom("dept", Operator.GT, 10),
                Atom("sdate", Operator.IN, [date(1995,1,1), date(1995,1,15)]),
                Atom("item_class", Operator.EQ, 5)
                ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            # The trick here is to recognize that "dept = 10 AND store = 51" is
            # fully redundant with "dept = 10 AND store > 50 AND store < 53".
            #
            # Both Postgres 18/HEAD and the transformed query use a single
            # index-only scan node, but the transformed version is much faster.
            # The performance difference is solely down to not using filter
            # quals at all, while pushing down "store > 50 AND store < 53" to
            # the nbtree code so that it won't read irrelevant "dept = 10"
            # tuples.
            "name": "Undetected redundant = condition",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.EQ, 10), Atom("store", Operator.GT, 50), Atom("store", Operator.LT, 53)]),
                AndOp([Atom("dept", Operator.EQ, 10), Atom("store", Operator.EQ, 51)])
                ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], # This will be set globally before coalesce
            "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "Complex AND/OR Predicate",
            "predicate_constructor": test_construct_complex_and_or_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 6
        },
        {
            "name": "Really Complex AND/OR Predicate",
            "predicate_constructor": test_construct_really_complex_and_or_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 5
        },
        {
            "name": "Super Complex AND/OR Predicate",
            "predicate_constructor": test_construct_super_complex_and_or_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 3
        },
        {
            "name": "Hairy AND/OR Predicate",
            "predicate_constructor": test_construct_hairy_and_or_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 11
        },
        {
            "name": "Range Shattering",
            "predicate_constructor": test_construct_expansion_logic_shattering_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 5
        },
        {
            "name": "Maximal Shatter then Coalesce",
            "predicate_constructor": test_construct_maximal_shatter_then_coalesce,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "IS_ANYTHING Propagation",
            "predicate_constructor": test_construct_is_anything_propagation,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "IN Expansion and Point Constraints",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.IN, [10, 20]),
                       Atom("sdate", Operator.EQ, date(1995, 2, 1)),
                       Atom("item_class", Operator.EQ, 5)]),
                AndOp([Atom("dept", Operator.EQ, 10),
                       Atom("sdate", Operator.EQ, date(1995, 3, 1)),
                       Atom("item_class", Operator.EQ, 5)])
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 2
        },
        {
            "name": "More IN Expansion Constraints",
            "predicate_constructor": lambda: OrOp([
                AndOp([Atom("dept", Operator.IN, [10, 20]),
                       Atom("sdate", Operator.EQ, date(1995, 2, 1)),
                       Atom("item_class", Operator.EQ, 5)]),
                AndOp([Atom("dept", Operator.EQ, 10),
                       Atom("sdate", Operator.EQ, date(1995, 3, 1)),
                       Atom("item_class", Operator.EQ, 8)])
            ]),
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 3
        },
        {
            "name": "Ordering Conflict",
            "predicate_constructor": test_construct_ordering_conflict,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 3,
            "expected_ordering_failure": True
        },
        {
            "name": "Ordering Conflict Later Col",
            "predicate_constructor": test_construct_ordering_conflict_later_col,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 3,
            "expected_ordering_failure": True
        },
        {
            "name": "No Ordering Conflict",
            "predicate_constructor": test_construct_no_ordering_conflict,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
        {
            "name": "Still No Ordering Conflict",
            "predicate_constructor": test_construct_still_no_ordering_conflict,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 3
        },
        {
            "name": "MergeRetrievals Gap Splitting",
            "predicate_constructor": test_construct_merge_retrievals_gap_splitting_predicate,
            "index_cols": ["dept", "sdate", "item_class", "store"], "table_name": TABLE_NAME,
            "expected_num_retrievals": 1
        },
    ]

    test_execution_summary = [] # To store {"name": test_name, "original_ms": original_time, "transformed_ms": mdam_time, "status": "PASSED/FAILED"}

    try:
        print(f"Attempting to connect to DB '{DB_CONNECTION_PARAMS['dbname']}' on {DB_CONNECTION_PARAMS['host']}...")
        conn_test = psycopg2.connect(**DB_CONNECTION_PARAMS)
        conn_test.close()
        print("DB connection OK. Running tests...")
        print(f"Logging transformed SQL queries to: {TRANSFORMED_SQL_LOG_FILE_PATH}")
        with open(TRANSFORMED_SQL_LOG_FILE_PATH, "w", encoding="utf-8") as log_file:
            passed_tests = []
            expected_failure_tests = []
            failed_tests = []
            for tc_idx, tc in enumerate(test_cases):
                status, name, orig_time, mdam_time, num_retrievals, mismatch_details, orig_idx_scan_nodes = \
                    run_test_case(DB_CONNECTION_PARAMS, tc, tc_idx + 1, log_file_tranformed=log_file)

                test_execution_summary.append({
                    "name": name,
                    "original_ms": orig_time,
                    "transformed_ms": mdam_time,
                    "num_retrievals": num_retrievals,
                    "status": status,
                    "mismatch_details": mismatch_details,
                    "original_index_scan_nodes": orig_idx_scan_nodes
                })

                if status == "PASSED":
                    passed_tests.append(name)
                elif status == "EXPECTED_FAILURE":
                    expected_failure_tests.append(name)
                else:
                    failed_tests.append(name)

            print("\n\n" + "="*20 + " TEST SUMMARY " + "="*20)
            print(f"Total tests run: {len(test_cases)}")
            print(f"Passed: {len(passed_tests)}")
            if expected_failure_tests:
                print(f"Expected failures: {len(expected_failure_tests)} (ordering conflicts)")
            print(f"Failed: {len(failed_tests)}")

            print("\n--- Test Results ---")
            NUM_TOTAL_TESTS = len(test_execution_summary)
            MAX_NUM_WIDTH = len(str(NUM_TOTAL_TESTS)) if NUM_TOTAL_TESTS > 0 else 1
            for i, summary_item in enumerate(test_execution_summary):
                status = summary_item["status"]
                if status == "PASSED":
                    icon = "PASS"
                elif status == "EXPECTED_FAILURE":
                    icon = "XFAIL"
                else:
                    icon = "FAIL"
                print(f"  {i+1:>{MAX_NUM_WIDTH}}. [{icon:>5}] {summary_item['name']}")

            if not failed_tests:
                print("\nAll tests produced expected results.")
            else:
                print(f"\n{len(failed_tests)} unexpected failure(s).")

            TITLE_TEXT = "EXECUTION TIME SUMMARY"
            HEADER_LINE_STR = f"{'Test Case Name':<35} | {'Original (ms)':>15} | {'Orig IdxScan Nodes':>19} | {'Transformed (ms)':>18} | {'Num Retrievals':>14} | {'Ratio (T/O)':>12}"
            TOTAL_WIDTH = len(HEADER_LINE_STR)

            PADDING_TOTAL = TOTAL_WIDTH - (len(TITLE_TEXT) + 2)
            PAD_LEFT = PADDING_TOTAL // 2
            PAD_RIGHT = TOTAL_WIDTH - (len(TITLE_TEXT) + 2 + PAD_LEFT)

            print(f"\n\n{'=' * PAD_LEFT} {TITLE_TEXT} {'=' * PAD_RIGHT}")
            print(HEADER_LINE_STR)
            print("-" * TOTAL_WIDTH)
            for summary_item in test_execution_summary:
                orig_t_str = f"{summary_item['original_ms']:.3f}" if summary_item['original_ms'] != -1.0 else "N/A"
                trans_t_str = f"{summary_item['transformed_ms']:.3f}" if summary_item['transformed_ms'] != -1.0 else "N/A"
                ORIG_IDX_SCAN_NODES_STR = f"{summary_item['original_index_scan_nodes']}" \
                    if summary_item['original_index_scan_nodes'] != -1 else "N/A"
                NUM_RET_STR = str(summary_item.get('num_retrievals', 'N/A'))

                RATIO_STR = "N/A"
                orig_val = summary_item['original_ms']
                trans_val = summary_item['transformed_ms']
                if orig_val > 0 and trans_val != -1.0 and trans_val >= 0:
                    RATIO_STR = f"{(trans_val / orig_val):.2f}"
                print(f"{summary_item['name']:<35} | {orig_t_str:>15} | {ORIG_IDX_SCAN_NODES_STR:>19} | {trans_t_str:>18} | {NUM_RET_STR:>14} | {RATIO_STR:>12}")
            print("=" * TOTAL_WIDTH)

            mismatch_warnings = []
            for summary_item in test_execution_summary:
                if summary_item["mismatch_details"]:
                    mismatch_warnings.append(summary_item)

            if mismatch_warnings:
                print(f"\n\n{'=' * PAD_LEFT} RETRIEVAL MISMATCH WARNINGS {'=' * PAD_RIGHT}")
                for item in mismatch_warnings:
                    expected, actual = item["mismatch_details"]
                    print(f"  ⚠️ Test '{item['name']}': Expected {expected} retrievals, but got {actual}.")
                print("=" * TOTAL_WIDTH)

    except psycopg2.OperationalError as e:
        print(f"\nCRITICAL: DB connection failed. Cannot run tests.\nParams: {DB_CONNECTION_PARAMS}\nError: {e}")
        print(f"Ensure PostgreSQL is running and accessible, and table '{TABLE_NAME}' exists with appropriate schema and data.")
