#!/usr/bin/env python3
import re
import psycopg2
import copy
import argparse
from typing import List, Tuple, Optional, Set
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/tmp/query_reducer.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class QueryReducer:
    def __init__(self, conn_params, table_name="fuzz_skip_scan", index_columns=("a", "b", "c", "d")):
        """
        Initialize the query reducer

        Args:
            table_name: Name of the table to test
            index_columns: Columns in the composite index
        """
        self.conn = psycopg2.connect(**conn_params)
        self.conn.autocommit = True
        self.table_name = table_name
        self.index_columns = index_columns

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def execute_query(self, query: str, force_seqscan: bool = False) -> (List[tuple], bool):
        """
        Execute a query and return results

        Args:
            query: SQL query to execute
            force_seqscan: Whether to force a sequential scan

        Returns:
            Tuple of (results list, error_occurred flag)
        """
        with self.conn.cursor() as cur:
            # Set plan options
            if force_seqscan:
                cur.execute("SET enable_indexscan = off; SET enable_bitmapscan = off;")
            else:
                cur.execute("SET enable_indexscan = on; SET enable_bitmapscan = off; SET enable_seqscan = off;")

            try:
                cur.execute(query)
                results = cur.fetchall()
                return results, False
            except Exception as e:
                logger.error(f"Query execution failed: {e}")
                return [], True
            finally:
                # Reset plan options
                cur.execute("RESET enable_indexscan; RESET enable_bitmapscan; RESET enable_seqscan;")

    def get_execution_plan(self, query: str, force_seqscan: bool = False) -> (dict, bool):
        """
        Get the execution plan for a query

        Args:
            query: SQL query
            force_seqscan: Whether to force a sequential scan

        Returns:
            Tuple of (execution plan as dict, error_occurred flag)
        """
        with self.conn.cursor() as cur:
            # Set plan options
            if force_seqscan:
                cur.execute("SET enable_indexscan = off; SET enable_bitmapscan = off;")
            else:
                cur.execute("SET enable_indexscan = on; SET enable_bitmapscan = off; SET enable_seqscan = off;")

            try:
                cur.execute(f"EXPLAIN (FORMAT JSON) {query}")
                plan = cur.fetchone()[0]
                return plan, False
            except Exception as e:
                logger.error(f"Failed to get execution plan: {e}")
                return {}, True
            finally:
                # Reset plan options
                cur.execute("RESET enable_indexscan; RESET enable_bitmapscan; RESET enable_seqscan;")

    def find_scan_node_type(self, plan: dict) -> str:
        """
        Recursively search through an execution plan to find the scan node type

        Args:
            plan: Execution plan node

        Returns:
            Scan node type (e.g., 'Index Scan', 'Seq Scan', etc.)
        """
        if not plan:
            return "Unknown"

        node_type = plan.get('Node Type', '')

        # If this is a scan node, return its type
        if 'Scan' in node_type:
            return node_type

        # Check child nodes
        for child_key in ['Plans', 'Plan']:
            if child_key in plan:
                child_plans = plan[child_key]
                if isinstance(child_plans, list):
                    for child in child_plans:
                        scan_type = self.find_scan_node_type(child)
                        if scan_type != "Unknown" and 'Scan' in scan_type:
                            return scan_type
                elif isinstance(child_plans, dict):
                    scan_type = self.find_scan_node_type(child_plans)
                    if scan_type != "Unknown" and 'Scan' in scan_type:
                        return scan_type

        return "Unknown"

    def verify_plan_type(self, query: str, expected_type: str) -> bool:
        """
        Verify that a query uses the expected scan type

        Args:
            query: SQL query
            expected_type: Expected scan type ('Index Scan' or 'Seq Scan')

        Returns:
            True if plan matches expected type
        """
        force_seqscan = (expected_type == 'Seq Scan')
        plan, error_occurred = self.get_execution_plan(query, force_seqscan)

        # If getting plan failed and we expected an index scan, this is actually fine
        # since it means the query has an error when using index scan - which is what we're testing for
        if error_occurred:
            if expected_type == 'Index Scan':
                logger.info("Index scan plan generation failed - likely due to btree bug")
                return True
            else:
                logger.warning("Failed to get sequential scan plan - cannot verify")
                return False

        if not plan or not plan[0].get('Plan'):
            return False

        # Recursively search for the scan node
        node_type = self.find_scan_node_type(plan[0]['Plan'])

        if expected_type == 'Index Scan' and 'Index' in node_type:
            return True
        elif expected_type == 'Seq Scan' and node_type == 'Seq Scan':
            return True

        logger.warning(f"Expected {expected_type}, but got {node_type}")
        return False

    def results_differ(self, query: str) -> bool:
        """
        Check if index scan and sequential scan results differ for the same query

        Args:
            query: SQL query to test

        Returns:
            True if results differ (including if index scan errors), False otherwise
        """
        # Get results with index scan
        index_results, index_error = self.execute_query(query, force_seqscan=False)

        # Get results with sequential scan (assumed to be correct)
        seq_results, seq_error = self.execute_query(query, force_seqscan=True)

        # If the sequential scan fails, we can't use this query for comparison
        if seq_error:
            logger.warning("Sequential scan failed - cannot use this query for comparison")
            return False

        # If index scan errors but seq scan succeeds, this is a bug!
        if index_error:
            logger.info("Index scan produced an error while sequential scan succeeded - this is a bug!")
            return True

        # Verify plans were as expected
        if not self.verify_plan_type(query, 'Index Scan') or not self.verify_plan_type(query, 'Seq Scan'):
            logger.warning("Couldn't verify scan types")
            return False

        # Convert to sets for comparison (ignoring order)
        index_set = set(index_results)
        seq_set = set(seq_results)

        if len(index_set) != len(seq_set):
            logger.info(f"Row count differs: index={len(index_set)}, seq={len(seq_set)}")
            return True

        if index_set != seq_set:
            logger.info("Result sets differ")
            return True

        return False

    def parse_where_conditions(self, query: str) -> List[str]:
        """
        Parse WHERE conditions from a query

        Args:
            query: SQL query

        Returns:
            List of individual conditions
        """
        # Find the WHERE clause
        where_match = re.search(r'WHERE\s+(.*?)(?:\s+ORDER BY|\s*$)', query, re.IGNORECASE | re.DOTALL)
        if not where_match:
            return []

        where_clause = where_match.group(1).strip()

        # Split on AND, but handle parentheses and IN clauses properly
        conditions = []
        current_cond = ""
        paren_depth = 0

        for char in where_clause:
            if char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1

            current_cond += char

            # Only split on AND when not within parentheses or IN clauses
            if current_cond.lower().endswith(' and ') and paren_depth == 0:
                conditions.append(current_cond[:-5].strip())
                current_cond = ""

        # Add the last condition
        if current_cond:
            conditions.append(current_cond.strip())

        return conditions

    def build_query(self, base_query: str, conditions: List[str]) -> str:
        """
        Build a query from base parts and conditions

        Args:
            base_query: Base query (SELECT and FROM parts)
            conditions: List of WHERE conditions

        Returns:
            Complete SQL query
        """
        # Extract ORDER BY if present
        order_by = ""
        order_match = re.search(r'ORDER BY\s+(.*?)(?:\s*$)', base_query, re.IGNORECASE)
        if order_match:
            order_by = f"ORDER BY {order_match.group(1)}"
            base_query = re.sub(r'ORDER BY\s+.*?(?:\s*$)', '', base_query)

        if conditions:
            where_clause = " AND ".join(conditions)
            query = f"{base_query} WHERE {where_clause}"
        else:
            query = base_query

        if order_by:
            query = f"{query} {order_by}"

        return query

    def extract_base_query(self, query: str) -> str:
        """
        Extract the base part of a query (SELECT and FROM)

        Args:
            query: SQL query

        Returns:
            Base query
        """
        # Remove WHERE and everything after it
        base_match = re.search(r'^(SELECT\s+.*?\s+FROM\s+.*?)(?:\s+WHERE|\s+ORDER BY|\s*$)',
                               query, re.IGNORECASE | re.DOTALL)
        if base_match:
            return base_match.group(1).strip()
        return query

    def extract_order_by(self, query: str) -> Optional[str]:
        """
        Extract ORDER BY clause from a query

        Args:
            query: SQL query

        Returns:
            ORDER BY clause or None
        """
        order_match = re.search(r'ORDER BY\s+(.*?)(?:\s*$)', query, re.IGNORECASE)
        if order_match:
            return f"ORDER BY {order_match.group(1)}"
        return None

    def mutate_in_clause(self, condition: str) -> List[str]:
        """
        Generate simpler versions of IN conditions

        Args:
            condition: SQL condition with IN clause

        Returns:
            List of simplified conditions
        """
        in_match = re.search(r'(.*?)\s+IN\s+\((.*?)\)', condition, re.IGNORECASE)
        if not in_match:
            return []

        col_name = in_match.group(1).strip()
        values = [v.strip() for v in in_match.group(2).split(',')]

        if len(values) <= 1:
            return []

        mutations = []

        # Try removing one value at a time
        for i in range(len(values)):
            new_values = values.copy()
            removed = new_values.pop(i)
            if new_values:
                new_condition = f"{col_name} IN ({', '.join(new_values)})"
                mutations.append(new_condition)
            else:
                # If only one value left, convert to equality
                new_condition = f"{col_name} = {removed}"
                mutations.append(new_condition)

        # Try keeping only the first half of values
        if len(values) > 2:
            first_half = values[:len(values)//2]
            new_condition = f"{col_name} IN ({', '.join(first_half)})"
            mutations.append(new_condition)

            # Try keeping only the second half of values
            second_half = values[len(values)//2:]
            new_condition = f"{col_name} IN ({', '.join(second_half)})"
            mutations.append(new_condition)

        return mutations

    def mutate_range_condition(self, condition: str) -> List[str]:
        """
        Generate simpler versions of range conditions using adaptive search

        Args:
            condition: SQL range condition

        Returns:
            List of simplified conditions
        """
        mutations = []

        # For conditions like "x >= N"
        gt_match = re.search(r'(.*?)\s*>=\s*(\d+)', condition)
        if gt_match:
            col = gt_match.group(1).strip()
            val = int(gt_match.group(2))
            # Try more aggressive jumps - halfway to a reasonable upper bound
            jump = max(1, (10000 - val) // 2)  # Assuming 10000 as a reasonable upper bound
            mutations.append(f"{col} >= {val + jump}")

        # For conditions like "x <= M"
        lt_match = re.search(r'(.*?)\s*<=\s*(\d+)', condition)
        if lt_match:
            col = lt_match.group(1).strip()
            val = int(lt_match.group(2))
            # Try more aggressive jumps - halfway to a reasonable lower bound
            jump = max(1, val // 2)  # Assuming 0 as a reasonable lower bound
            mutations.append(f"{col} <= {val - jump}")

        # For "x > N"
        gt_strict_match = re.search(r'(.*?)\s*>\s*(\d+)', condition)
        if gt_strict_match:
            col = gt_strict_match.group(1).strip()
            val = int(gt_strict_match.group(2))
            # Try more aggressive jumps
            jump = max(1, (10000 - val) // 2)
            mutations.append(f"{col} > {val + jump}")

        # For "x < N"
        lt_strict_match = re.search(r'(.*?)\s*<\s*(\d+)', condition)
        if lt_strict_match:
            col = lt_strict_match.group(1).strip()
            val = int(lt_strict_match.group(2))
            # Try more aggressive jumps
            jump = max(1, val // 2)
            mutations.append(f"{col} < {val - jump}")

        return mutations

    def adaptive_range_search(self, base_query: str, conditions: List[str],
                              index: int, condition: str) -> (bool, str):
        """
        Perform adaptive binary search to quickly find the tightest inequality bound
        that still reproduces the bug

        Args:
            base_query: Base query (SELECT and FROM parts)
            conditions: List of WHERE conditions
            index: Index of the condition to modify
            condition: Current condition with inequality

        Returns:
            (success, new_query) tuple
        """
        # Extract column name and original value
        original_conditions = conditions.copy()
        order_by = self.extract_order_by(base_query) or ""

        # Handle different inequality types
        for pattern, operator in [
            (r'(.*?)\s*>=\s*(\d+)', '>='),
            (r'(.*?)\s*<=\s*(\d+)', '<='),
            (r'(.*?)\s*>\s*(\d+)', '>'),
            (r'(.*?)\s*<\s*(\d+)', '<')
        ]:
            match = re.search(pattern, condition)
            if not match:
                continue

            col = match.group(1).strip()
            current_val = int(match.group(2))

            # Set search range based on operator
            if operator in ['>', '>=']:
                # We want to increase the value for > and >= operators
                low = current_val
                high = current_val + 100000  # Set a reasonable upper limit
                best_val = current_val
                increasing = True
            else:  # '<', '<='
                # We want to decrease the value for < and <= operators
                low = 0  # Set a reasonable lower limit
                high = current_val
                best_val = current_val
                increasing = False

            # Binary search to find the tightest bound
            while high - low > 1:
                mid = (low + high) // 2
                test_val = mid

                # Create test condition
                test_condition = f"{col} {operator} {test_val}"

                # Create test query
                test_conditions = original_conditions.copy()
                test_conditions[index] = test_condition

                if test_conditions:
                    test_query = f"{base_query} WHERE {' AND '.join(test_conditions)}"
                else:
                    test_query = base_query

                if order_by:
                    test_query = f"{test_query} {order_by}"

                # Check if this tighter bound still reproduces the bug
                if self.results_differ(test_query):
                    # We can tighten further
                    if increasing:
                        low = mid  # We can increase the bound further
                        best_val = mid
                    else:
                        high = mid  # We can decrease the bound further
                        best_val = mid
                else:
                    # Too tight, relax bound
                    if increasing:
                        high = mid
                    else:
                        low = mid

            # Create final condition with best value
            final_condition = f"{col} {operator} {best_val}"

            # Create final query
            final_conditions = original_conditions.copy()
            final_conditions[index] = final_condition

            if final_conditions:
                final_query = f"{base_query} WHERE {' AND '.join(final_conditions)}"
            else:
                final_query = base_query

            if order_by:
                final_query = f"{final_query} {order_by}"

            # Final check
            if self.results_differ(final_query) and best_val != current_val:
                logger.info(f"Found optimized bound: {final_condition}")
                return True, final_query

            break

        return False, ""

    def reduce_query(self, original_query: str, max_iterations: int = 100) -> str:
        """
        Reduce a query to its simplest form that still reproduces the bug

        Args:
            original_query: SQL query to reduce
            max_iterations: Maximum iterations to attempt

        Returns:
            Simplified query
        """
        # Verify original query has the bug
        if not self.results_differ(original_query):
            logger.error("Original query does not exhibit the bug!")
            return original_query

        logger.info("Original query confirmed to have different results between scan methods")

        current_query = original_query
        base_query = self.extract_base_query(original_query)
        order_by = self.extract_order_by(original_query)

        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            logger.info(f"=== Iteration {iteration} ===")
            logger.info(f"Current query: {current_query}")

            # Parse current conditions
            conditions = self.parse_where_conditions(current_query)
            logger.info(f"Found {len(conditions)} conditions")

            # Try removing each condition completely
            found_simpler = False
            for i, condition in enumerate(conditions):
                logger.info(f"Trying to remove condition: {condition}")

                # Skip the condition
                new_conditions = conditions.copy()
                new_conditions.pop(i)

                # Build and test new query
                if new_conditions:
                    test_query = f"{base_query} WHERE {' AND '.join(new_conditions)}"
                else:
                    test_query = base_query

                if order_by:
                    test_query = f"{test_query} {order_by}"

                # Check if simplified query still shows the bug
                if self.results_differ(test_query):
                    logger.info("Success! Found simpler query that still has the bug")
                    current_query = test_query
                    found_simpler = True
                    break

            # If we couldn't remove any condition completely, try to simplify individual conditions
            if not found_simpler and conditions:
                for i, condition in enumerate(conditions):
                    logger.info(f"Trying to simplify condition: {condition}")

                    # First try adaptive range search for inequalities
                    if any(op in condition for op in ['>', '<', '>=', '<=']):
                        logger.info("Performing adaptive search for inequality bounds")
                        success, optimized_query = self.adaptive_range_search(
                            base_query, conditions, i, condition
                        )
                        if success:
                            logger.info("Adaptive search successful")
                            current_query = optimized_query
                            found_simpler = True
                            break

                    # If adaptive search didn't work or wasn't applicable, try standard mutations
                    mutations = []

                    # Handle IN clauses
                    if ' IN ' in condition.upper():
                        mutations.extend(self.mutate_in_clause(condition))

                    # Handle range conditions with more aggressive jumps
                    if any(op in condition for op in ['>', '<', '>=', '<=']):
                        mutations.extend(self.mutate_range_condition(condition))

                    # Handle IS NOT NULL
                    if 'IS NOT NULL' in condition.upper():
                        # Try removing this condition
                        continue

                    # Try each mutation
                    for mutation in mutations:
                        logger.info(f"Testing mutation: {mutation}")

                        new_conditions = conditions.copy()
                        new_conditions[i] = mutation

                        test_query = f"{base_query} WHERE {' AND '.join(new_conditions)}"
                        if order_by:
                            test_query = f"{test_query} {order_by}"

                        if self.results_differ(test_query):
                            logger.info(f"Success! Found simpler condition: {mutation}")
                            current_query = test_query
                            found_simpler = True
                            break

                    if found_simpler:
                        break

            # If we couldn't simplify anything, we're done
            if not found_simpler:
                logger.info("No further simplifications possible")
                break

        logger.info(f"=== Reduction complete ===")
        logger.info(f"Original query: {original_query}")
        logger.info(f"Reduced query: {current_query}")

        # Get row counts for both queries
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM ({original_query}) AS q")
            original_count = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM ({current_query}) AS q")
            reduced_count = cur.fetchone()[0]

        logger.info(f"Original query returned {original_count} rows")
        logger.info(f"Reduced query returns {reduced_count} rows")

        return current_query


def main():
    # Connection parameters - adjust as needed
    conn_params = {
        "host": "localhost",
        "database": "regression",
        "user": "pg",
    }
    parser = argparse.ArgumentParser(description='Reduce a PostgreSQL query that demonstrates a btree bug')
    parser.add_argument('--query', required=True, help='SQL query to reduce')
    parser.add_argument('--table', default='fuzz_skip_scan', help='Table name')
    parser.add_argument('--index-cols', default='a,b,c,d', help='Comma-separated list of index columns')
    parser.add_argument('--max-iterations', type=int, default=50_000, help='Maximum iterations')

    args = parser.parse_args()

    index_columns = args.index_cols.split(',')

    reducer = QueryReducer(
        conn_params=conn_params,
        table_name=args.table,
        index_columns=index_columns
    )

    try:
        reduced_query = reducer.reduce_query(args.query, args.max_iterations)
        print("\nFinal reduced query:")
        print(reduced_query)
    finally:
        reducer.close()


if __name__ == "__main__":
    main()
