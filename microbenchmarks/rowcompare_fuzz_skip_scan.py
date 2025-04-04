#!/usr/bin/env python3
import math
import psycopg2
import random
import time
import traceback

order_by_forward = " ORDER BY a, b, c, d"
order_by_backward = " ORDER BY a DESC, b DESC, c DESC, d DESC"
order_by_forward_nullsfirst = " ORDER BY a NULLS FIRST, b NULLS FIRST, c NULLS FIRST, d NULLS FIRST"
order_by_backward_nullsfirst = " ORDER BY a DESC NULLS LAST, b DESC NULLS LAST, c DESC NULLS LAST, d DESC NULLS LAST"

def biased_random_int(min_val, max_val):
    """
    Generate a random integer between min_val and max_val (inclusive),
    with reduced probability near the boundaries.

    Args:
        min_val: Minimum possible value
        max_val: Maximum possible value

    Returns:
        int: A random integer with reduced probability near the bounds
    """
    # Use beta distribution to create a bell-shaped probability curve
    # Alpha = Beta = 2 gives a parabolic shape with lower probability at extremes
    alpha = 2
    beta = 2

    # Generate a random value between 0 and 1 with beta distribution
    random_val = random.betavariate(alpha, beta)

    # Scale to our range and round to integer
    scaled_val = min_val + random_val * (max_val - min_val)
    return round(scaled_val)

class PostgreSQLSkipScanTester:
    def __init__(self, conn_params, table_name, num_rows, num_samples,
                 num_tests, report_interval, gap_row_compare_probability=0.3):
        self.conn_params = conn_params
        self.table_name = table_name
        self.num_rows = num_rows
        self.num_samples = num_samples
        self.num_tests = num_tests
        self.report_interval = report_interval
        self.columns = ['a', 'b', 'c', 'd']
        self.equality_operators = ['=', 'IN', 'IS NULL']
        self.inequality_operators = ['<', '<=', '>=', '>', 'IS NOT NULL']
        self.row_compare_operators = ['<', '<=', '>=', '>']
        self.conn = None
        self.gap_row_compare_probability = gap_row_compare_probability

    def connect(self):
        """Establish connection to PostgreSQL database"""
        try:
            self.conn = psycopg2.connect(**self.conn_params)
            print("Successfully connected")
        except Exception as e:
            print(f"Connection error: {e}")
            traceback.print_exc()
            raise

    def setup_test_environment(self):
        """Create test table and populate with random data"""
        try:
            cursor = self.conn.cursor()

            try:
                # First, check if the table exists in the database
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = %s
                    )
                """, (self.table_name,))
                table_exists = cursor.fetchone()[0]

                if table_exists:
                    # If table exists, check if it has the right number of rows
                    cursor.execute(f"SELECT count(*) FROM {self.table_name}")
                    idx_results = cursor.fetchall()
                    if idx_results[0][0] == self.num_rows:
                        print("Found preexisting table loaded from previous run")
                        return

            except Exception as e:
                pass

            # Create test table
            cursor.execute(f"DROP TABLE IF EXISTS {self.table_name}")
            cursor.execute(f"""
                create table {self.table_name} (
                    id serial primary key,
                    a integer,
                    b integer,
                    c integer,
                    d integer)
            """)

            # Create the composite index first (makes suffix truncation
            # effective)
            cursor.execute(f"""
                CREATE INDEX idx_{self.table_name}_abcd
                ON {self.table_name} (a, b, c, d)
            """)
            cursor.execute(f"""
                CREATE INDEX idx_{self.table_name}_abcd_nulls_first
                ON {self.table_name} (a nulls first, b nulls first, c nulls first, d nulls first)
            """)
            cursor.execute(f"""
                alter table {self.table_name} set (parallel_workers=0);
            """)
            # Insert random data (with some NULL values)
            for _ in range(self.num_rows):
                # Randomly decide whether to include NULL values for each column
                a_val = None if random.random() < 0.05 else random.randint(1, 20)
                b_val = None if random.random() < 0.05 else random.randint(1, 20)
                c_val = None if random.random() < 0.05 else random.randint(1, 100)
                d_val = None if random.random() < 0.05 else random.randint(1, 10_000)

                cursor.execute(f"""
                    INSERT INTO {self.table_name} (a, b, c, d)
                    VALUES (%s, %s, %s, %s)
                """, (a_val, b_val, c_val, d_val))

            self.conn.commit()

            # VACUUM
            self.conn.set_session(autocommit=True) # So that we can run VACUUM, etc
            cursor.execute(f"""
                vacuum analyze {self.table_name}
            """)

            print(f"Created test table {self.table_name} with {self.num_rows} rows")
            cursor.execute(f"""
                set max_parallel_workers_per_gather to 0;
            """)

        except Exception as e:
            self.conn.rollback()
            print(f"Setup error: {e}")
            traceback.print_exc()
            raise

    def get_column_value_range(self, column):
        """Returns the value range for a given column."""
        column_ranges = {
            'a': (-1, 21),
            'b': (-1, 21),
            'c': (-1, 101),
            'd': (-1, 10_001)
        }
        return column_ranges.get(column, (-1, 21))

    def generate_cond(self, column, operator_type=None,
                      eq_weights = [0.78, 0.20, 0.02], # Use IS NULL much less often
                      ineq_weights = [0.20, 0.20, 0.20, 0.17, 0.03]):   # Use IS NOT NULL much less often
        """
        Generate a single condition for a column using the specified operator type.

        Args:
            column: The column name to generate condition for
            operator_type: 'equality', 'inequality', or None (random)

        Returns:
            A condition string like "a > 5" or "b IS NULL"
        """
        if operator_type == 'equality':
            op = random.choices(self.equality_operators, weights=eq_weights)[0]
        elif operator_type == 'inequality':
            available_operators_and_weights = [(self.inequality_operators[i], ineq_weights[i])
                                               for i in range(len(self.inequality_operators)) if ineq_weights[i] > 0]

            if not available_operators_and_weights:
                op = random.choice(self.inequality_operators)
            else:
                operators, weights = zip(*available_operators_and_weights)
                op = random.choices(operators, weights=weights)[0]
        else:
            all_operators = self.equality_operators + self.inequality_operators
            op = random.choice(all_operators)

        if op in ('IS NULL', 'IS NOT NULL'):
            return f"{column} {op}"

        value_range = self.get_column_value_range(column)

        if op == 'IN':
            nelements = random.randint(2, 20)
            elements = {biased_random_int(*value_range) for _ in range(nelements)}
            values_str = ", ".join(str(x) for x in sorted(elements))
            return f"{column} IN ({values_str})"

        value = biased_random_int(*value_range)
        return f"{column} {op} {value}"

    def generate_row_compare_cond(self, use_gaps=False, specific_columns=None, first_element_not_null=True):
        """
        Generate a row compare condition like "(a, b, c) > (1, 2, 3)" or "(a, c) > (1, 3)".
        Returns the condition string, the list of columns, the list of actual values, and the operator.
        """
        if specific_columns:
            rc_columns = specific_columns
            num_rc_cols = len(specific_columns)
        else:
            num_rc_cols = random.randint(2, 4)
            if use_gaps:
                rc_columns = sorted(random.sample(self.columns, num_rc_cols), key=self.columns.index)
            else:
                rc_columns = self.columns[:num_rc_cols]

        op = random.choice(self.row_compare_operators)

        values = []
        for i, col in enumerate(rc_columns):
            if i == 0 and first_element_not_null:
                # Ensure the first element is never NULL
                val = biased_random_int(*self.get_column_value_range(col))
            else:
                val = None if random.random() < 0.02 else biased_random_int(*self.get_column_value_range(col))
            values.append(val)

        columns_str = ", ".join(rc_columns)
        values_str = ", ".join(["NULL" if v is None else str(v) for v in values])
        return f"({columns_str}) {op} ({values_str})", rc_columns, values, op

    def generate_complementary_row_compare(self, rc_columns, base_operator, base_values, use_gaps):
        """
        Generates a complementary row compare condition that does not contradict
        the base_operator and base_values, while aiming for a wider range.
        """
        # Determine the complementary operator
        if base_operator in ['>', '>=']:
            complementary_op_candidates = ['<', '<=']
            is_upper_bound = True
        else: # base_operator in ['<', '<=']
            complementary_op_candidates = ['>', '>=']
            is_upper_bound = False

        complementary_op = random.choice(complementary_op_candidates)

        new_values = list(base_values)

        # Find the index of the first non-None value that we can meaningfully adjust.
        # This will be the pivot for creating the complementary condition.
        pivot_idx = -1
        for i in range(len(base_values)):
            if base_values[i] is not None:
                pivot_idx = i
                break

        if pivot_idx == -1: # This should not happen if first_element_not_null is true for the base
            # Fallback, just generate random values for all.
            return self.generate_row_compare_cond(use_gaps=use_gaps, specific_columns=rc_columns, first_element_not_null=True)[0:4] # Returns clause, cols, values, op

        # Generate a random offset (e.g., 1 to 5)
        offset = random.randint(1, 5)

        # Adjust the pivot value
        col_name = rc_columns[pivot_idx]
        min_val, max_val = self.get_column_value_range(col_name)
        current_pivot_val = base_values[pivot_idx]

        if is_upper_bound: # Base was >, >=; need <, <=
            # Complementary value should be greater than base_values[pivot_idx]
            # We want to create a range like (X, NULL) > (V1, NULL) AND (X, NULL) < (V2, NULL)
            # where V2 > V1. If V1 is X, V2 must be X+1 at minimum.
            if complementary_op == '<':
                # New value needs to be > base_value. If base_value is an integer, new_value must be at least base_value + 1.
                target_value = current_pivot_val + offset
            else: # complementary_op == '<='
                # New value needs to be >= base_value. If base_value is an integer, new_value must be at least base_value.
                target_value = current_pivot_val + offset -1 # Allow for target value to be equal or greater

            # Ensure target_value is within bounds and actually allows for a range
            new_pivot_val = max(current_pivot_val + 1, min(max_val, target_value))
            if current_pivot_val >= new_pivot_val and complementary_op == '<':
                new_pivot_val = current_pivot_val + 1 # Force a valid upper bound
            elif current_pivot_val > new_pivot_val and complementary_op == '<=':
                new_pivot_val = current_pivot_val # Allow equality or force a valid upper bound

            new_values[pivot_idx] = new_pivot_val

            # For subsequent values, try to set them to their minimum possible value (within range)
            # to make the row compare less restrictive
            for j in range(pivot_idx + 1, len(rc_columns)):
                col_j = rc_columns[j]
                new_values[j] = self.get_column_value_range(col_j)[0] + 1 if random.random() < 0.5 else None # Randomly null or min
        else: # Base was <, <=; need >, >=
            # Complementary value should be smaller than base_values[pivot_idx]
            if complementary_op == '>':
                # New value needs to be < base_value. If base_value is an integer, new_value must be at most base_value - 1.
                target_value = current_pivot_val - offset
            else: # complementary_op == '>='
                # New value needs to be <= base_value. If base_value is an integer, new_value must be at most base_value.
                target_value = current_pivot_val - offset + 1 # Allow for target value to be equal or less

            # Ensure target_value is within bounds and actually allows for a range
            new_pivot_val = min(current_pivot_val - 1, max(min_val, target_value))
            if current_pivot_val <= new_pivot_val and complementary_op == '>':
                 new_pivot_val = current_pivot_val - 1 # Force a valid lower bound
            elif current_pivot_val < new_pivot_val and complementary_op == '>=':
                new_pivot_val = current_pivot_val # Allow equality or force a valid lower bound

            new_values[pivot_idx] = new_pivot_val

            # For subsequent values, try to set them to their maximum possible value (within range)
            # to make the row compare less restrictive
            for j in range(pivot_idx + 1, len(rc_columns)):
                col_j = rc_columns[j]
                new_values[j] = self.get_column_value_range(col_j)[1] - 1 if random.random() < 0.5 else None # Randomly null or max

        # Ensure the first value in new_values is not NULL
        if new_values[0] is None:
            new_values[0] = biased_random_int(*self.get_column_value_range(rc_columns[0]))

        # Final check for contradiction between base and new values considering NULLs at later positions
        # This is a simplified check for integer type first elements with NULLs following.
        # If (A, NULL) > (X, NULL) AND (A, NULL) <= (Y, NULL), and X >= Y, it's contradictory.
        if base_values[0] is not None and new_values[0] is not None:
            if base_operator in ['>', '>='] and complementary_op in ['<', '<=']:
                # Lower bound: (base_values[0], ...) and Upper bound: (new_values[0], ...)
                if base_values[0] > new_values[0]: # X > Y
                    # Contradiction: Adjust new_values[0] to be greater than base_values[0]
                    new_values[0] = base_values[0] + 1
                elif base_values[0] == new_values[0]:
                    # (X, NULL) > (Y, NULL) and (X, NULL) <= (Y, NULL) where X=Y -> Contradictory for integers.
                    # Or (X, NULL) >= (Y, NULL) and (X, NULL) < (Y, NULL) where X=Y -> Contradictory for integers.
                    if (base_operator == '>' and complementary_op == '<=') or \
                       (base_operator == '>=' and complementary_op == '<'):
                        new_values[0] = base_values[0] + 1 # Force separation for distinct integer ranges
            elif base_operator in ['<', '<='] and complementary_op in ['>', '>=']:
                # Upper bound: (base_values[0], ...) and Lower bound: (new_values[0], ...)
                if base_values[0] < new_values[0]: # Y < X
                    # Contradiction: Adjust new_values[0] to be smaller than base_values[0]
                    new_values[0] = base_values[0] - 1
                elif base_values[0] == new_values[0]:
                     if (base_operator == '<' and complementary_op == '>=') or \
                        (base_operator == '<=' and complementary_op == '>'):
                        new_values[0] = base_values[0] - 1 # Force separation for distinct integer ranges

        columns_str = ", ".join(rc_columns)
        values_str = ", ".join(["NULL" if v is None else str(v) for v in new_values])
        return f"({columns_str}) {complementary_op} ({values_str})", new_values, complementary_op

    def find_matching_operator_indices(self, all_conditions):
        matching_indices = set()
        related_pairs = [(0, 1), (2, 3)]

        for index, operator in enumerate(self.inequality_operators):
            for dynamic_string in all_conditions:
                if operator in dynamic_string:
                    matching_indices.add(index)
                    for pair in related_pairs:
                        if index in pair:
                            matching_indices.update(pair)
                    break
        return matching_indices

    def sort_constraints(self, constraints):
        operator_priority = {
            ">": 0,
            ">=": 1,
            "<": 2,
            "<=": 3
        }

        def get_sort_key(constraint):
            parts = constraint.split()
            if '(' in constraint and ')' in constraint and len(parts) >= 2:
                cols_part = constraint[constraint.find('(')+1:constraint.find(')')]
                columns_in_rc = [c.strip() for c in cols_part.split(',')]
                column = columns_in_rc[0]
                op_index = constraint.find(')') + 2
                operator = constraint[op_index:op_index+2].strip()
                return (column, operator_priority.get(operator, 999))
            elif len(parts) >= 2:
                column = parts[0]
                operator = parts[1]
                return (column, operator_priority.get(operator, 999))
            return (constraint, 999)

        return sorted(constraints, key=get_sort_key)

    def resolve_contradictions(self, conditions):
        parsed_conditions = []
        for condition in conditions:
            if '(' in condition and ')' in condition:
                parsed_conditions.append((None, None, None, condition))
                continue

            parts = condition.split()
            # Only process numeric comparisons
            if len(parts) == 3 and parts[1] in ['>', '<', '>=', '<=']:
                try:
                    column = parts[0]
                    operator = parts[1]
                    value = int(parts[2])
                    parsed_conditions.append((column, operator, value, condition))
                except ValueError:
                    # If value isn't an integer, just keep the original condition
                    parsed_conditions.append((None, None, None, condition))
            else:
                # For non-comparison conditions like "IS NOT NULL"
                parsed_conditions.append((None, None, None, condition))

        # Group conditions by column
        column_conditions = {}
        for col, op, val, cond in parsed_conditions:
            if col is not None:
                if col not in column_conditions:
                    column_conditions[col] = []
                column_conditions[col].append((op, val, cond))

        # Check and resolve contradictions
        modified_conditions = conditions.copy()

        for column, col_conditions in column_conditions.items():
            lower_bounds = []  # > and >=
            upper_bounds = []  # < and <=

            # Separate into lower and upper bounds
            for op, val, cond in col_conditions:
                if op in ['>', '>=']:
                    lower_bounds.append((op, val, cond))
                elif op in ['<', '<=']:
                    upper_bounds.append((op, val, cond))

            # Check for contradictions between lower and upper bounds
            for lower_op, lower_val, lower_cond in lower_bounds:
                for upper_op, upper_val, upper_cond in upper_bounds:
                    # Contradiction if lower bound >= upper bound
                    is_contradiction = False

                    if lower_op == '>' and upper_op == '<' and lower_val >= upper_val:
                        is_contradiction = True
                    elif lower_op == '>' and upper_op == '<=' and lower_val >= upper_val:
                        is_contradiction = True
                    elif lower_op == '>=' and upper_op == '<' and lower_val >= upper_val:
                        is_contradiction = True
                    elif lower_op == '>=' and upper_op == '<=' and lower_val > upper_val:
                        is_contradiction = True

                    # If contradiction, swap the values
                    if is_contradiction:
                        # Create new conditions with swapped values
                        new_lower_cond = f"{column} {lower_op} {upper_val}"
                        new_upper_cond = f"{column} {upper_op} {lower_val}"

                        if lower_cond in modified_conditions:
                            modified_conditions[modified_conditions.index(lower_cond)] = new_lower_cond
                        if upper_cond in modified_conditions:
                            modified_conditions[modified_conditions.index(upper_cond)] = new_upper_cond

        return modified_conditions

    def generate_random_where_clause(self):
        all_conditions = []
        used_columns = set()
        query_metadata = {
            'is_row_compare': False,
            'is_gap_row_compare': False,
            'row_compare_columns': []
        }

        # Decide whether to include a row compare condition (single, pair, or none)
        row_compare_choice = random.choices(['none', 'single', 'pair'], weights=[0.4, 0.1, 0.5])[0]

        if row_compare_choice != 'none':
            use_gaps = random.random() < self.gap_row_compare_probability
            try:
                # Generate the first row compare condition
                rc1_clause, rc_columns, rc1_values, rc1_op = self.generate_row_compare_cond(
                    use_gaps=use_gaps, first_element_not_null=True
                )
                all_conditions.append(rc1_clause)
                used_columns.update(rc_columns)
                query_metadata['is_row_compare'] = True
                query_metadata['row_compare_columns'] = rc_columns
                if rc_columns != self.columns[self.columns.index(rc_columns[0]):self.columns.index(rc_columns[0]) + len(rc_columns)]:
                    query_metadata['is_gap_row_compare'] = True

                if row_compare_choice == 'pair':
                    # Generate a complementary row compare condition
                    rc2_clause, rc2_values, rc2_op = self.generate_complementary_row_compare(
                        rc_columns, rc1_op, rc1_values, use_gaps
                    )
                    # Add rc2_clause only if it's genuinely complementary and not identical to rc1_clause
                    if rc1_clause != rc2_clause:
                        all_conditions.append(rc2_clause)

            except ValueError as e:
                print(f"Warning: {e}. Skipping row compare for this query.")

        remaining_columns = [col for col in self.columns if col not in used_columns]

        if not remaining_columns and not all_conditions:
            remaining_columns = [random.choice(self.columns)]

        if remaining_columns:
            num_columns_to_cond = random.randint(0, len(remaining_columns))
            columns_for_scalar_cond = random.sample(remaining_columns, num_columns_to_cond)

            if not columns_for_scalar_cond and not all_conditions:
                 columns_for_scalar_cond = [random.choice(self.columns)]

            if len(columns_for_scalar_cond) == 1 and columns_for_scalar_cond[0] == 'a' and not all_conditions:
                if 'b' in self.columns and 'b' not in used_columns:
                    columns_for_scalar_cond[0] = 'b'
                elif len(self.columns) > 1:
                    other_cols = [c for c in self.columns if c != 'a' and c not in used_columns]
                    if other_cols:
                        columns_for_scalar_cond[0] = random.choice(other_cols)

            columns_with_equality = set()

            for col in columns_for_scalar_cond:
                if random.random() < 0.5:
                    condition = self.generate_cond(col, 'equality')
                    all_conditions.append(condition)
                    columns_with_equality.add(col)
                else:
                    pass

            for col in columns_for_scalar_cond:
                if col not in columns_with_equality:
                    weights = [0.25, 0.7, 0.05]
                    num_conditions = random.choices([1, 2, 3], weights=weights)[0]
                    ineq_weights_base = [0.20, 0.20, 0.20, 0.17, 0.03]

                    for _ in range(num_conditions):
                        current_ineq_weights = list(ineq_weights_base)
                        zero_weights_indices = self.find_matching_operator_indices(all_conditions)

                        for idx in zero_weights_indices:
                            if idx < len(current_ineq_weights):
                                current_ineq_weights[idx] = 0

                        condition = self.generate_cond(col, 'inequality',
                                                       ineq_weights=current_ineq_weights)
                        all_conditions.append(condition)

        # If we somehow ended up with no conditions (unlikely), add one
        if not all_conditions:
            col = random.choice(self.columns)
            all_conditions.append(self.generate_cond(col))

        all_conditions = self.sort_constraints(all_conditions)
        all_conditions = self.resolve_contradictions(all_conditions)
        if random.random() < 0.50:
            where_clause_str = " AND ".join(all_conditions) + (order_by_forward if random.random() < 0.50 else
                                                               order_by_backward)
        else:
            where_clause_str = " AND ".join(all_conditions) + (order_by_forward_nullsfirst if random.random() < 0.50 else
                                                               order_by_backward_nullsfirst)
        return where_clause_str, query_metadata

    def execute_test_query(self, where_clause):
        """Execute a test query with both sequential scan and index scan"""
        cursor = self.conn.cursor()

        # Force sequential scan
        cursor.execute("SET enable_indexscan = off; SET enable_bitmapscan = off;")
        seq_query = f"EXPLAIN ANALYZE SELECT * FROM {self.table_name} WHERE {where_clause}"
        cursor.execute(seq_query)
        seq_plan = cursor.fetchall()

        # Get sequential scan results
        cursor.execute(f"SELECT * FROM {self.table_name} WHERE {where_clause}")
        seq_results = cursor.fetchall()

        # Force index scan
        cursor.execute("SET enable_indexscan = on; SET enable_seqscan = off; SET enable_bitmapscan = off;")
        idx_query = f"EXPLAIN ANALYZE SELECT * FROM {self.table_name} WHERE {where_clause}"
        cursor.execute(idx_query)
        idx_plan = cursor.fetchall()

        # Get index scan results
        cursor.execute(f"SELECT * FROM {self.table_name} WHERE {where_clause}")
        idx_results = cursor.fetchall()

        # Reset scan settings
        cursor.execute("RESET enable_indexscan; RESET enable_seqscan; RESET enable_bitmapscan;")

        return {
            'where_clause': where_clause,
            'seq_plan': seq_plan,
            'idx_plan': idx_plan,
            'seq_results': seq_results,
            'idx_results': idx_results,
            'results_match': sorted(seq_results) == sorted(idx_results),
            'seq_count': len(seq_results),
            'idx_count': len(idx_results)
        }

    def verify_scan_results(self, test_result):
        """Verify that sequential scan and index scan results match"""
        if not test_result['results_match']:
            print("\n❌ TEST FAILED: Results do not match!")
            print(f"Query: SELECT * FROM {self.table_name} WHERE {test_result['where_clause']}")
            print(f"Sequential scan found {test_result['seq_count']} rows")
            print(f"Index scan found {test_result['idx_count']} rows")
            return False
        return True

    def run_fuzzing_queries(self):
        """Run a batch of random test queries and verify results"""
        print(f"\nRunning {self.num_tests} random test queries...")

        start_time = time.time()
        failures = 0
        multiple_inequality_count = 0
        row_compare_count = 0
        gap_row_compare_count = 0

        for i in range(1, self.num_tests + 1):
            where_clause, metadata = self.generate_random_where_clause()

            # Count queries with multiple inequalities on the same column (fixed)
            multiple_inequalities = False
            for column in self.columns:
                # Count occurrences of inequality operators for this column
                ninequalities_for_column = sum(1 for op in ['<', '<=', '>=', '>', 'IS NOT NULL']
                                               if f"{column} {op}" in where_clause and not (f"({column}" in where_clause and ")" in where_clause))
                if ninequalities_for_column > 1:
                    multiple_inequalities = True
                    break

            if multiple_inequalities:
                multiple_inequality_count += 1

            # Use the metadata directly from generate_random_where_clause()
            if metadata['is_row_compare']:
                row_compare_count += 1
            if metadata['is_gap_row_compare']:
                gap_row_compare_count += 1

            test_result = self.execute_test_query(where_clause)

            if not self.verify_scan_results(test_result):
                failures += 1

            if i % self.report_interval == 0:
                print(f"Completed {i} tests. Failures: {failures}")

        end_time = time.time()
        duration = end_time - start_time

        print(f"\nCompleted {self.num_tests} tests in {duration:.2f} seconds")
        print(f"Queries with multiple inequalities on the same column: {multiple_inequality_count}")
        print(f"Queries with row compare conditions: {row_compare_count}")
        print(f"Queries with gap row compare conditions: {gap_row_compare_count}")
        print(f"Total failures: {failures}")

        if failures == 0:
            print("✅ All tests passed!")
        else:
            print(f"❌ {failures} tests failed!")

        return failures == 0

    def dump_plan_samples(self):
        """Analyze and print execution plans for a few sample queries"""
        print(f"\nAnalyzing execution plans for {self.num_samples} sample queries...")

        for i in range(self.num_samples):
            where_clause, _ = self.generate_random_where_clause()
            test_result = self.execute_test_query(where_clause)

            print(f"\nQuery {i+1}: SELECT * FROM {self.table_name} WHERE {where_clause}")
            print("\nSequential scan plan:")
            for line in test_result['seq_plan']:
                print(line[0])

            print("\nIndex scan plan:")
            for line in test_result['idx_plan']:
                print(line[0])

            print(f"\nResults match: {test_result['results_match']}")
            print(f"Row count: {test_result['seq_count']}")
            print("-" * 80)

    def cleanup(self):
        """Close connection"""
        if self.conn:
            try:
                cursor = self.conn.cursor()
                self.conn.commit()
                self.conn.close()
                print(f"Closed connection")
            except Exception as e:
                print(f"Cleanup error: {e}")
                traceback.print_exc()

    def run_all_tests(self):
        """Run all test types"""
        try:
            self.connect()
            self.setup_test_environment()

            # Analyze some sample execution plans first, to preview the work
            # that run_fuzzing_queries() will do (runs quickly)
            self.dump_plan_samples()

            # The real work happens in run_fuzzing_queries() (takes a while)
            return self.run_fuzzing_queries()

        except Exception as e:
            print(f"Test error: {e}")
            traceback.print_exc()
            return False
        finally:
            self.cleanup()


if __name__ == "__main__":
    # Connection parameters - adjust as needed
    conn_params = {
        "host": "localhost",
        "database": "regression",
        "user": "pg",
    }

    # Create and run the tester
    tester = PostgreSQLSkipScanTester(
        conn_params=conn_params,
        table_name="fuzz_skip_scan",
        num_rows=150_000, # rows in `table_name` table
        num_samples=10, # Number of plan samples to dump (previews test query structure)
        num_tests=5_000_000, # Number of test queries
        report_interval=1000, # Report progress each time this many queries run
        gap_row_compare_probability=0.1
    )

    success = tester.run_all_tests()

    if success:
        print("\n✅ All tests completed successfully")
    else:
        print("\n❌ Test failures detected")
