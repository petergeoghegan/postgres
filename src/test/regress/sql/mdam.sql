--
-- MDAM (Multi-Dimensional Access Method) OR-clause optimization tests
--
-- Tests the transformation of complex OR predicates on multi-column B-tree
-- indexes into non-overlapping index scan retrievals combined via Append.
--

-- Setup: create test table with multi-column index
CREATE TABLE mdam_test_tbl (
    a int,
    b int,
    c int,
    d int
);

-- Insert enough rows for the optimizer to prefer index scans
INSERT INTO mdam_test_tbl
SELECT i / 1000, i % 100, i % 50, i % 25
FROM generate_series(1, 100000) i;

CREATE INDEX mdam_test_tbl_idx ON mdam_test_tbl (a, b, c, d);
ANALYZE mdam_test_tbl;

-- Disable seqscan and parallelism so we can see index plan choices clearly
SET enable_seqscan = off;
SET enable_bitmapscan = off;
SET max_parallel_workers_per_gather = 0;

--
-- Single-column index: OR of ranges that overlap.  Merge step should
-- coalesce [1,100], [50,200], and {150} into the single range [1,200],
-- producing one ordered Index Only Scan (no Append, since after merging
-- there is only one retrieval left).
--
EXPLAIN (COSTS OFF)
SELECT oid FROM pg_class
WHERE oid BETWEEN 1 AND 100 OR oid BETWEEN 50 AND 200 OR oid = 150;

--
-- Same single-retrieval case but with ORDER BY DESC.  The merged range
-- can be served by a backward Index Only Scan with no Sort on top.
--
EXPLAIN (COSTS OFF)
SELECT oid FROM pg_class
WHERE oid BETWEEN 100 AND 300 OR oid BETWEEN 200 AND 3000
ORDER BY oid DESC;

--
-- Single-column index: OR of ranges that do not overlap.  [1,100] and
-- [102,200] are disjoint; the equality oid = 150 is absorbed into the
-- second range.  Result is an Append with two ordered IndexOnlyScan
-- children, instead of the BitmapOr plan we would otherwise get.
--
EXPLAIN (COSTS OFF)
SELECT oid FROM pg_class
WHERE oid BETWEEN 1 AND 100 OR oid BETWEEN 102 AND 200 OR oid = 150;

--
-- A predicate the MDAM design is fundamentally unable to handle while also
-- preserving index key space order.  Make sure that MDAM machinery bails.
--
-- Shape: a non-point leading-column constraint shared between OR arms
-- whose disjuncts split on *different* later index columns.  Here 'a > 5'
-- is the shared non-point constraint, and the OR splits on 'c' vs 'd':
--
--   a > 5 AND (c = 10 OR d = 5)
--
-- DNF:
--   (a > 5, c = 10)
--   (a > 5, d = 5)
--
-- Within the shared 'a > 5' range any single 'a' value can carry rows
-- from either disjunct, and those rows interleave column-by-column down
-- the key.  The only way to expose that ordering through an Append of
-- independent IndexScans would be to expand 'a > 5' into one EQ per
-- distinct value of 'a' present in the table -- a runtime-unknown,
-- potentially unbounded number of disjuncts.  There is no static
-- reformulation in terms of nbtree's CNF scankeys + ScalarArrayOps that
-- achieves the same effect, so MDAM declines the transformation and
-- leaves the predicate for bitmap OR (or, with bitmap disabled, a plain
-- index scan with a filter qual on the OR).
--
EXPLAIN (COSTS OFF)
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a > 5 AND (c = 10 OR d = 5);

SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a > 5 AND (c = 10 OR d = 5);

-- This similar-ish query can be transformed by MDAM machinery, since it has
-- its = conditions are on same lower-order column:
EXPLAIN (COSTS OFF)
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a > 5 AND (c = 3 OR c = 7);

SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a > 5 AND (c = 3 OR c = 7);

--
-- Basic: OR on leading column with different EQ values (non-overlapping)
-- Each arm constrains different columns of the same index.
-- MDAM should produce Append of IndexOnlyScans.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);

-- Verify correctness: compare MDAM result with seqscan
SELECT a, b, c, d FROM mdam_test_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20)
ORDER BY a, b, c, d;

--
-- Overlapping ranges on same column should merge into single tight scan.
-- BETWEEN 4 AND 7 OR BETWEEN 5 AND 11 => single scan with >= 4 AND <= 11
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE a BETWEEN 4 AND 7 OR a BETWEEN 5 AND 11;

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE a BETWEEN 4 AND 7 OR a BETWEEN 5 AND 11;
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a BETWEEN 4 AND 7 OR a BETWEEN 5 AND 11;

--
-- Non-overlapping ranges: should produce Append of two scans
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE a BETWEEN 4 AND 7 OR a BETWEEN 9 AND 11;

--
-- Mixed depth: one arm has 1 column, other has 2 columns.
-- Tests that expansion correctly preserves all column constraints.
-- The second arm must keep BOTH a>=9 AND a<=11 (not just a>=9).
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a BETWEEN 4 AND 7) OR (a BETWEEN 9 AND 11 AND b = 1);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a BETWEEN 4 AND 7) OR (a BETWEEN 9 AND 11 AND b = 1);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a BETWEEN 4 AND 7) OR (a BETWEEN 9 AND 11 AND b = 1);

--
-- Three-arm OR: all on same index, non-overlapping leading column
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND b = 10 AND c = 3)
   OR (a = 5 AND b = 20 AND c = 7)
   OR (a = 10 AND c = 5);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 5 AND b = 10 AND c = 3)
   OR (a = 5 AND b = 20 AND c = 7)
   OR (a = 10 AND c = 5);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 5 AND b = 10 AND c = 3)
   OR (a = 5 AND b = 20 AND c = 7)
   OR (a = 10 AND c = 5);

--
-- Contradiction detection: OR arm that simplifies to empty
-- a IN (10,20) AND a > 25 => contradiction, so only one arm survives
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a IN (10, 20) AND a > 25) OR (a = 5 AND b = 3);

--
-- GUC test: enable_mdam = off should fall back to BitmapOr
--
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);
SET enable_mdam = on;
SET enable_bitmapscan = off;

--
-- Redundant OR: one arm is subset of the other.
-- a BETWEEN 3 AND 10 fully contains a = 5, so should merge to single scan.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE a BETWEEN 3 AND 10 OR a = 5;

--
-- IN list in OR arm: tests SAOP handling during DNF extraction
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND b IN (1, 2, 3)) OR (a = 10 AND b = 50);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 5 AND b IN (1, 2, 3)) OR (a = 10 AND b = 50);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 5 AND b IN (1, 2, 3)) OR (a = 10 AND b = 50);

--
-- Multiple ANDed ORs on separate columns should collapse to one scan with IN lists.
-- (a=1 OR a=2) AND (b=10 OR b=20) AND (c=3 OR c=4) => single scan
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 1 OR a = 2) AND (b = 10 OR b = 20) AND (c = 3 OR c = 4);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 1 OR a = 2) AND (b = 10 OR b = 20) AND (c = 3 OR c = 4);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 1 OR a = 2) AND (b = 10 OR b = 20) AND (c = 3 OR c = 4);

--
-- Complementary ranges on a nullable column whose union covers all non-NULL
-- values but excludes NULL.  Merging to "no scan key on c" would scan c IS
-- NULL rows too, which the original predicate excludes (strict comparisons
-- evaluate to NULL/false for NULL inputs).  Without an explicit IS NULL arm,
-- MDAM cannot drop the c constraint; the OR is left as a Filter on top of the
-- a-only index scan.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 10 AND c > 5) OR (a = 10 AND c <= 5);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 10 AND c > 5) OR (a = 10 AND c <= 5);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 10 AND c > 5) OR (a = 10 AND c <= 5);

--
-- EQ-to-IN folding: multiple arms sharing same prefix, differing on trailing col.
-- Should fold the trailing EQ values into an IN list.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND b = 10 AND c = 1 AND d = 0)
   OR (a = 5 AND b = 10 AND c = 1 AND d = 5)
   OR (a = 5 AND b = 10 AND c = 2 AND d = 0)
   OR (a = 5 AND b = 10 AND c = 2 AND d = 5)
   OR (a = 10 AND b = 20 AND c = 3 AND d = 0);

--
-- IN expansion then coalesce roundtrip: leading IN expands then merges back.
-- a IN (1,2) AND b IN (10,20) AND c=3 => should stay as 1 retrieval
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE a IN (1, 2) AND b IN (10, 20) AND c = 3 AND d > 10;

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE a IN (1, 2) AND b IN (10, 20) AND c = 3 AND d > 10;
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE a IN (1, 2) AND b IN (10, 20) AND c = 3 AND d > 10;

--
-- Ordering conflict: non-point constraint on leading col + different later cols.
-- (a > 10 AND b = 5) OR (a > 10 AND c = 3) -- MDAM should reject (fall back).
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a > 10 AND b = 5) OR (a > 10 AND c = 3);

--
-- No ordering conflict: same structure but leading col is point constraint.
-- (a = 10 AND b = 5) OR (a = 10 AND c = 3) -- MDAM can handle this.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 10 AND b = 5) OR (a = 10 AND c = 3);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 10 AND b = 5) OR (a = 10 AND c = 3);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 10 AND b = 5) OR (a = 10 AND c = 3);

--
-- Middle column gap: arm skips column b entirely.
-- Should still produce Append after expanding the unconstrained b.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 10 AND c = 5)
   OR (a = 10 AND b = 20 AND c = 7)
   OR (a = 20 AND c = 1);

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a = 10 AND c = 5)
   OR (a = 10 AND b = 20 AND c = 7)
   OR (a = 20 AND c = 1);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a = 10 AND c = 5)
   OR (a = 10 AND b = 20 AND c = 7)
   OR (a = 20 AND c = 1);

--
-- MDAM paper example (simplified to 4-column int index).
-- Deeply nested AND/OR with IN lists and ranges -- the flagship test.
--
-- Original predicate:
--   ((c = 10 AND b BETWEEN 4 AND 25) OR a IN (2, 4, 5))
--   AND
--   ((a = 4 AND c = 5) OR (c IN (5, 10) AND (b = 4 OR a = 2)))
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE ((c = 10 AND b >= 4 AND b <= 25) OR a IN (2, 4, 5))
  AND ((a = 4 AND c = 5) OR (c IN (5, 10) AND (b = 4 OR a = 2)));

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE ((c = 10 AND b >= 4 AND b <= 25) OR a IN (2, 4, 5))
  AND ((a = 4 AND c = 5) OR (c IN (5, 10) AND (b = 4 OR a = 2)));
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE ((c = 10 AND b >= 4 AND b <= 25) OR a IN (2, 4, 5))
  AND ((a = 4 AND c = 5) OR (c IN (5, 10) AND (b = 4 OR a = 2)));

--
-- Date column type: MDAM must handle non-integer types correctly.
--
CREATE TABLE mdam_date_tbl (dept int, sdate date, item_class int, store int);
INSERT INTO mdam_date_tbl
  SELECT i/100, '1995-01-01'::date + (i % 400), i % 75, i % 300
  FROM generate_series(1, 100000) i;
CREATE INDEX mdam_date_tbl_idx ON mdam_date_tbl(dept, sdate, item_class, store);
ANALYZE mdam_date_tbl;

-- MDAM paper flagship query shape (adapted for date column).
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_date_tbl
WHERE ((item_class = 10 AND sdate >= '1995-06-04' AND sdate <= '1995-06-25')
       OR dept IN (2, 4, 5))
  AND ((dept = 4 AND item_class = 5)
       OR (item_class IN (5, 10) AND (sdate = '1995-06-04' OR dept = 2)));

-- Verify correctness
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_date_tbl
WHERE ((item_class = 10 AND sdate >= '1995-06-04' AND sdate <= '1995-06-25')
       OR dept IN (2, 4, 5))
  AND ((dept = 4 AND item_class = 5)
       OR (item_class IN (5, 10) AND (sdate = '1995-06-04' OR dept = 2)));
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_date_tbl
WHERE ((item_class = 10 AND sdate >= '1995-06-04' AND sdate <= '1995-06-25')
       OR dept IN (2, 4, 5))
  AND ((dept = 4 AND item_class = 5)
       OR (item_class IN (5, 10) AND (sdate = '1995-06-04' OR dept = 2)));

DROP TABLE mdam_date_tbl;

--
-- Non-index conjunct passed through as per-branch qpqual.
--
-- ((A1 AND A2 AND A3) OR (B1 AND B2)) AND filter_on_unindexed_column
-- should still produce an MDAM Append: the OR portion drives index access on
-- each branch, and the unindexed filter becomes a Filter on each IndexScan.
--
CREATE TABLE mdam_filter_tbl (a int, b int, c int, total int);
INSERT INTO mdam_filter_tbl
SELECT i / 1000, i % 100, i % 50, i FROM generate_series(1, 100000) i;
CREATE INDEX mdam_filter_tbl_idx ON mdam_filter_tbl (a, b, c);
ANALYZE mdam_filter_tbl;

-- Without the extra filter: Index Only Scan baseline.
EXPLAIN (COSTS OFF)
SELECT a, b, c FROM mdam_filter_tbl
WHERE (a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20);

-- With the extra filter: same Append shape, plain Index Scan with Filter.
EXPLAIN (COSTS OFF)
SELECT a, b, c FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000;

-- Verify correctness against bitmap fallback.
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000;
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000;

-- Multiple non-index conjuncts ANDed in.
EXPLAIN (COSTS OFF)
SELECT a, b, c FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000
  AND total < 90000;

SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000
  AND total < 90000;
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_filter_tbl
WHERE ((a = 5 AND b = 10 AND c = 3) OR (a = 5 AND c = 20))
  AND total > 50000
  AND total < 90000;

-- Negative case: unindexable predicate buried INSIDE an OR arm.
-- MDAM cannot drop the predicate (would lose rows), so it must bail out
-- on this index entirely and let some other path handle the query.
EXPLAIN (COSTS OFF)
SELECT a, b, c FROM mdam_filter_tbl
WHERE (a = 5 AND total > 50000) OR (a = 10 AND b = 20);

DROP TABLE mdam_filter_tbl;

--
-- Cross-product contradiction: outer ANDed predicate contradicts one OR arm.
-- Mirrors the MDAM-paper shape (and the sales_mdam_paper test fixture):
-- two OR arms differing on the leading index column, plus an outer ANDed
-- equality on a middle index column that contradicts a value in the first
-- arm.  Skip-scan can't factor here because dept differs across arms.
--   ((dept=1 AND sdate=D1 AND item_class=4) OR (dept=50 AND sdate=D2))
--     AND item_class=5
-- After distributing AND over OR:
--   arm 1: dept=1  AND sdate=D1 AND item_class=4 AND item_class=5  -- contradictory
--   arm 2: dept=50 AND sdate=D2 AND item_class=5                   -- consistent
-- The contradictory arm is kept in the DNF.  Critically, the merge step
-- preserves the contradictory column atoms (item_class=4 AND item_class=5)
-- as real index quals on the IndexScan, instead of dropping them and
-- letting only item_class=5 survive as a per-branch Filter (which would
-- scan thousands of unnecessary index entries for the dept=1 prefix).
-- nbtree's _bt_preprocess_keys detects the inconsistency at runtime.
--
CREATE TABLE mdam_paper_tbl (dept int, sdate date, item_class int, store int);
INSERT INTO mdam_paper_tbl
  SELECT (i % 100) + 1,
         '1995-01-01'::date + ((i / 100) % 400),
         (i % 75) + 1,
         (i % 300) + 1
  FROM generate_series(1, 200000) i;
CREATE INDEX mdam_paper_idx ON mdam_paper_tbl(dept, sdate, item_class, store);
ANALYZE mdam_paper_tbl;

EXPLAIN (COSTS OFF)
SELECT dept, sdate, item_class, store FROM mdam_paper_tbl
WHERE ((dept = 1 AND sdate = '1995-03-04' AND item_class = 4)
    OR (dept = 50 AND sdate = '1995-09-04'))
  AND item_class = 5;

-- Verify correctness against the bitmap fallback.
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_paper_tbl
WHERE ((dept = 1 AND sdate = '1995-03-04' AND item_class = 4)
    OR (dept = 50 AND sdate = '1995-09-04'))
  AND item_class = 5;
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_paper_tbl
WHERE ((dept = 1 AND sdate = '1995-03-04' AND item_class = 4)
    OR (dept = 50 AND sdate = '1995-09-04'))
  AND item_class = 5;

DROP TABLE mdam_paper_tbl;

--
-- Shattering truncation: complex OR predicates can produce a cross-product of
-- elementary intervals that exceeds MDAM_MAX_RETRIEVALS (256) during the
-- recursive enumeration in mdam_generate_recursive.  Without an explicit
-- truncation guard, the recursion silently stopped early, producing an
-- incomplete Append that returned wrong row counts (caught by the fuzz
-- tester).  The fix marks ctx->retrievals_truncated and bails MDAM out
-- cleanly so the planner falls back to a correct plan (BitmapOr/SeqScan).
--
-- This query has critical-point counts a={3,5,13,65,89,96}, b={50},
-- c={25}, d={3,6,9,14,21,23} -> 13 x 3 x 3 x 13 = 1521 elementary-interval
-- combinations, far above the 256 cap.  Result must match the bitmap
-- oracle exactly regardless of which plan the planner chose.
--
SET enable_bitmapscan = on;
-- With MDAM enabled, the planner must NOT pick the (truncated, incomplete)
-- MDAM Append; the plan should fall back to a bitmap or other complete plan.
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_test_tbl
WHERE (a IN (3, 5, 13, 65, 89) AND b = 50 AND c = 25
       AND d IN (3, 6, 9, 14, 21, 23))
   OR (a < 96 AND b >= 50);
SET enable_mdam = off;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a IN (3, 5, 13, 65, 89) AND b = 50 AND c = 25
       AND d IN (3, 6, 9, 14, 21, 23))
   OR (a < 96 AND b >= 50);
SET enable_mdam = on;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a IN (3, 5, 13, 65, 89) AND b = 50 AND c = 25
       AND d IN (3, 6, 9, 14, 21, 23))
   OR (a < 96 AND b >= 50);
SET enable_bitmapscan = off;

--
-- Fuzz-derived: two-arm OR where one arm leaves the leading index column
-- wide open.  Index is (q, p, s, r); arm 1 constrains q (leading); arm 2
-- constrains only p (second column).  With NULLs in q, the shattered
-- partition over the leading column (q <= 95, q = 95, q > 95) silently
-- drops rows where q IS NULL but the open arm's non-leading quals match
-- (p < 42 AND r >= 5).  Larger DNFs can absorb the gap during merge, but
-- a two-arm OR cannot, so MDAM bails and the planner falls back to a
-- correct (though disabled) seq scan here.
--
CREATE TABLE mdam_fuzz_tbl (p int, q int, r int, s int);
INSERT INTO mdam_fuzz_tbl
SELECT NULLIF((i*13+7) % 80, 0), NULLIF((i*7+3) % 100, 0),
       NULLIF((i*11+5) % 60, 0), NULLIF((i*3+11) % 30, 0)
FROM generate_series(1, 100000) i;
CREATE INDEX mdam_fuzz_qpsr ON mdam_fuzz_tbl(q, p, s, r);
ANALYZE mdam_fuzz_tbl;

SET enable_bitmapscan = on;
SET enable_mdam = off;
SELECT count(*) AS expect FROM mdam_fuzz_tbl
WHERE ((q = 95 AND r >= 54) OR (p < 42 AND r >= 5));
SET enable_mdam = on;
SET enable_bitmapscan = off;
SET enable_indexonlyscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_fuzz_tbl
WHERE ((q = 95 AND r >= 54) OR (p < 42 AND r >= 5));
SELECT count(*) AS actual FROM mdam_fuzz_tbl
WHERE ((q = 95 AND r >= 54) OR (p < 42 AND r >= 5));
RESET enable_indexonlyscan;

DROP TABLE mdam_fuzz_tbl;

--
-- Fuzz-derived: documents an inherent limitation of the MDAM
-- transformation.  Minimal reduction of a failure on (p, q, r, s)
-- with predicate
--   (p < 70 AND r = 19) OR (p > 53 AND q >= 68 AND r < 55).
-- Distilled to two columns and two arms:
--   (a < 70 AND b = 19) OR (a > 53 AND b >= 68)
-- Critical points on the leading column a are {53, 70}.  After
-- shattering and column-0 splitting, the cleanest Append shape MDAM
-- could produce has four sub-paths:
--   (a <= 53,    b = 19)
--   (a in (53, 70), b = 19)
--   (a in (53, 70), b >= 68)
--   (a >= 70,    b >= 68)
-- but sub-paths 2 and 3 share the same a stripe (53, 70).  Append
-- concatenates -- it can't interleave -- so sub-path 2 emits its
-- whole stripe (a = 54..69 with b = 19) before sub-path 3 even
-- starts (a = 54..69 with b >= 68).  At the boundary, column a
-- "rewinds" from 69 to 54: a key-space ordering violation that no
-- Append shape can fix.  The required ordering is column-a
-- interleaving across the two non-leading constraints, which only a
-- single Index Scan with the OR as a filter could deliver.
--
-- MDAM correctly detects the rewind via the post-coalesce ordering
-- check and bails.  The planner falls back to BitmapOr.
--
CREATE TABLE mdam_open_arm_tbl (a int, b int);
INSERT INTO mdam_open_arm_tbl
SELECT i % 100, i % 80 FROM generate_series(1, 10000) i;
CREATE INDEX mdam_open_arm_tbl_idx ON mdam_open_arm_tbl (a, b);
ANALYZE mdam_open_arm_tbl;

SET enable_bitmapscan = on;

EXPLAIN (COSTS OFF)
SELECT * FROM mdam_open_arm_tbl
WHERE (a < 70 AND b = 19) OR (a > 53 AND b >= 68);

-- Coverage check: row counts match the bitmap fallback.
SET enable_mdam = off;
SELECT count(*) AS expect FROM mdam_open_arm_tbl
WHERE (a < 70 AND b = 19) OR (a > 53 AND b >= 68);
SET enable_mdam = on;
SELECT count(*) AS actual FROM mdam_open_arm_tbl
WHERE (a < 70 AND b = 19) OR (a > 53 AND b >= 68);

SET enable_bitmapscan = off;

DROP TABLE mdam_open_arm_tbl;

--
-- Fuzz-derived: MDAM must not place a scan key on a column that the
-- original arm constrained zero times.  When another arm forces a
-- critical point on column k inside an overlapping leading-column
-- partition, MDAM currently shatters the silent arm into {k<v, k=v,
-- k>v} sub-paths.  Each sub-path applies a strict operator to k that
-- the original arm never wrote, so any row whose k IS NULL falls
-- through every sub-path even though it satisfied the original arm
-- (which never referenced k at all).
--
-- Minimal reduction (from a 4-column failure on (p, q, r, s)):
--   (p = 70 AND r > 11 AND s > 12)
--     OR
--   (p > 61 AND q = 5 AND r > 49 AND s <= 20)
-- Arm 1 is silent on q.  Arm 2's q = 5 is the only critical point on
-- q, and the p = 70 partition is shared by both arms with disjoint
-- r/s constraints, forcing MDAM to shatter arm 1 there into
-- {q<5, q=5, q>5}.  Rows with q IS NULL match the original arm 1
-- but match no sub-path, so MDAM's row count is short by exactly
-- the q-IS-NULL fraction of arm 1's selectivity.
--
CREATE TABLE mdam_open_col_tbl (p int, q int, r int, s int);
INSERT INTO mdam_open_col_tbl
SELECT NULLIF((i*13+7) % 80, 0), NULLIF((i*7+3) % 100, 0),
       NULLIF((i*11+5) % 60, 0), NULLIF((i*3+11) % 30, 0)
FROM generate_series(1, 100000) i;
CREATE INDEX mdam_open_col_tbl_idx ON mdam_open_col_tbl (p, q, r, s);
ANALYZE mdam_open_col_tbl;

-- Plan check: MDAM bails; planner falls back to BitmapOr.
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_open_col_tbl
WHERE (p = 70 AND r > 11 AND s > 12)
   OR (p > 61 AND q = 5 AND r > 49 AND s <= 20);

-- Coverage check: row count must match the bitmap reference.
SET enable_mdam = off;
SELECT count(*) AS expect FROM mdam_open_col_tbl
WHERE (p = 70 AND r > 11 AND s > 12)
   OR (p > 61 AND q = 5 AND r > 49 AND s <= 20);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_open_col_tbl
WHERE (p = 70 AND r > 11 AND s > 12)
   OR (p > 61 AND q = 5 AND r > 49 AND s <= 20);

DROP TABLE mdam_open_col_tbl;

--
-- Fuzz-derived: SAOP at the last constrained column of a path must be
-- split into individual EQ paths before sort, otherwise the SAOP's
-- effective sort interval [min, max] encloses an adjacent path whose
-- range falls between the SAOP's values, and Append concatenation
-- emits the SAOP's high endpoint before the enclosed range -- a
-- key-space ordering violation that nothing in the Append fixes.
--   (a IN (10, 50)) OR (a >= 5 AND b = 25 AND c = 25)
-- Arm 1 is a SAOP whose last constrained column is the leading col a;
-- arm 2 produces a (10, 50) range stripe on a after shattering.  The
-- expected Append has five sub-paths: a in [5, 10), a = 10, a in
-- (10, 50), a = 50, a > 50.
--
CREATE TABLE mdam_saop_tbl (a int, b int, c int, d int);
INSERT INTO mdam_saop_tbl
SELECT i / 1000, i % 100, i % 50, i % 25 FROM generate_series(1, 100000) i;
CREATE INDEX mdam_saop_tbl_idx ON mdam_saop_tbl (a, b, c, d);
ANALYZE mdam_saop_tbl;

EXPLAIN (COSTS OFF)
SELECT * FROM mdam_saop_tbl
WHERE (a IN (10, 50)) OR (a >= 5 AND b = 25 AND c = 25)
ORDER BY a, b, c, d;

-- Ordering check: no row may have a strictly larger leading-col value
-- than the row immediately following it in the Append's output.
SELECT count(*) AS misordered_pairs FROM (
    SELECT a, lead(a) OVER () AS next_a FROM mdam_saop_tbl
    WHERE (a IN (10, 50)) OR (a >= 5 AND b = 25 AND c = 25)
) sub
WHERE next_a IS NOT NULL AND next_a < a;

-- Coverage check: row counts match the bitmap fallback.
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_saop_tbl
WHERE (a IN (10, 50)) OR (a >= 5 AND b = 25 AND c = 25);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_saop_tbl
WHERE (a IN (10, 50)) OR (a >= 5 AND b = 25 AND c = 25);

--
-- Fuzz-derived: a contradictory OR arm with an EQ + SAOP on the same
-- column (here b = 50 AND b IN (10, 80)) must not be silently widened
-- by step-4 expand/sort/coalesce.  The extract_interval ->
-- interval_to_atoms round-trip used by the multi-atom Range branch
-- collapses a SAOP to its [min,max] hull; when that hull contains the
-- conflicting EQ value (50 lies inside [10, 80]) the intersection
-- looks non-empty and the SAOP just vanishes from the resulting index
-- quals, so the Append child would match real rows the original
-- predicate excluded.  Both atoms must survive verbatim so nbtree's
-- runtime _bt_preprocess_keys detects the inconsistency.
--
-- b = 25 picks rows whose c is also 25 (since c = i % 50 and b = i %
-- 100), so the over-permissive scan that drops the SAOP would happily
-- return those rows under (c >= 19 AND c < 38).  With the SAOP
-- preserved verbatim, nbtree sees b = 25 AND b = ANY ('{10,80}')
-- together and short-circuits to zero rows -- matching the bitmap
-- oracle below.
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_saop_tbl
WHERE b = 25 AND ((a = 1 AND c > 1000)
              OR (b IN (10, 80) AND c < 38 AND c >= 19));
SELECT count(*) AS actual FROM mdam_saop_tbl
WHERE b = 25 AND ((a = 1 AND c > 1000)
              OR (b IN (10, 80) AND c < 38 AND c >= 19));

SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_saop_tbl
WHERE b = 25 AND ((a = 1 AND c > 1000)
              OR (b IN (10, 80) AND c < 38 AND c >= 19));
SELECT count(*) AS expect FROM mdam_saop_tbl
WHERE b = 25 AND ((a = 1 AND c > 1000)
              OR (b IN (10, 80) AND c < 38 AND c >= 19));
SET enable_mdam = on;
SET enable_bitmapscan = off;

DROP TABLE mdam_saop_tbl;

--
-- Fuzz-derived: ordering check must reject Append shapes where two
-- sub-paths' intervals strictly overlap on any column at first_diff
-- (the column where their sort keys diverge), not just on the leading
-- column.  The merge step can coalesce one conjunct's stripes into a
-- wider range when they share the same atoms on the *other* columns;
-- another conjunct that pinned that column with disjoint later-column
-- atoms now lives "inside" the wider range, and Append concatenation
-- emits one full sub-path before any of the other -- so the second
-- sub-path "rewinds" the column at the boundary.
--
-- Two cases below.  The first has the divergence at the leading
-- column; the second pins a=10 in two arms (so first_diff = 1, the
-- non-leading b column) but their b intervals strictly overlap.
--
CREATE TABLE mdam_overlap_tbl (a int, b int, c int, d int);
INSERT INTO mdam_overlap_tbl
SELECT (i / 1000) % 30, (i / 13) % 100, (i / 17) % 50, (i / 19) % 25
FROM generate_series(1, 200000) i;
CREATE INDEX mdam_overlap_tbl_idx ON mdam_overlap_tbl (a, b, c, d);
ANALYZE mdam_overlap_tbl;

-- (a) Leading-column overlap.
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_overlap_tbl
WHERE ((a < 37 AND b IN (26, 28, 33, 80) AND c > 10)
    OR (a > 11 AND b >= 81 AND c > 27))
ORDER BY a, b, c, d;

SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_overlap_tbl
WHERE ((a < 37 AND b IN (26, 28, 33, 80) AND c > 10)
    OR (a > 11 AND b >= 81 AND c > 27));
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_overlap_tbl
WHERE ((a < 37 AND b IN (26, 28, 33, 80) AND c > 10)
    OR (a > 11 AND b >= 81 AND c > 27));

-- (b) Non-leading-column overlap: a=10 in two arms, b intervals
-- (-inf, 50) and (30, +inf) overlap on (30, 50) with disjoint c
-- atoms.  A third arm (a=20...) keeps the planner from factoring
-- a=10 out of the OR.
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_overlap_tbl
WHERE ((a = 10 AND b < 50 AND c < 10 AND d > 2)
    OR (a = 10 AND b > 30 AND c >= 20 AND d > 3)
    OR (a = 20 AND b > 5 AND c < 5))
ORDER BY a, b, c, d;

SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_overlap_tbl
WHERE ((a = 10 AND b < 50 AND c < 10 AND d > 2)
    OR (a = 10 AND b > 30 AND c >= 20 AND d > 3)
    OR (a = 20 AND b > 5 AND c < 5));
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_overlap_tbl
WHERE ((a = 10 AND b < 50 AND c < 10 AND d > 2)
    OR (a = 10 AND b > 30 AND c >= 20 AND d > 3)
    OR (a = 20 AND b > 5 AND c < 5));

DROP TABLE mdam_overlap_tbl;

--
-- Fuzz-derived: a contradictory cross-product conjunct (e.g. one whose
-- per-column intersection of SAOP and EQ atoms is empty) is stashed in
-- ctx->contradictory and re-emitted as a standalone retrieval so the
-- Append remains in 1:1 correspondence with the original OR's arms --
-- nbtree's _bt_preprocess_keys then short-circuits the contradiction at
-- runtime.  Step-3 merge must not pass such a retrieval through
-- mdam_extract_interval, because that helper collapses a SAOP to its
-- closed [min,max] range and would silently lose the discrete value
-- constraint, producing an over-broad index condition that admits rows
-- the original OR never matched.
--
-- Reduction:
--   (a IN (3, 8, 15) AND b <= 10 AND c IN (1, 5, 9) AND c = 2)
--     OR
--   (a >= 20 AND b IN (5, 15, 25) AND c = 2)
-- Arm 1's c constraints intersect to empty.  Without preservation, step 3
-- would emit (a in [3,15] AND b <= 10 AND c = 2) -- losing the a- and
-- c-SAOPs entirely and admitting ~130 spurious rows.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a IN (3, 8, 15) AND b <= 10 AND c IN (1, 5, 9) AND c = 2)
   OR (a >= 20 AND b IN (5, 15, 25) AND c = 2);

-- Coverage check: the contradictory arm contributes zero rows; arm 2's
-- (a >= 20, b in {5,15,25}, c=2) is also empty for this data because
-- b in {5,15,25} forces c in {5,15,25} (b = i%100, c = i%50).  So a
-- correct MDAM result is zero; the bug would return ~130.
SET enable_mdam = off;
SET enable_bitmapscan = on;
SELECT count(*) AS expect FROM mdam_test_tbl
WHERE (a IN (3, 8, 15) AND b <= 10 AND c IN (1, 5, 9) AND c = 2)
   OR (a >= 20 AND b IN (5, 15, 25) AND c = 2);
SET enable_mdam = on;
SET enable_bitmapscan = off;
SELECT count(*) AS actual FROM mdam_test_tbl
WHERE (a IN (3, 8, 15) AND b <= 10 AND c IN (1, 5, 9) AND c = 2)
   OR (a >= 20 AND b IN (5, 15, 25) AND c = 2);

--
-- Only 2+ key column indexes qualify: single-column index should not trigger MDAM
--
CREATE TABLE mdam_single_col_tbl (x int);
CREATE INDEX ON mdam_single_col_tbl (x);
INSERT INTO mdam_single_col_tbl SELECT i FROM generate_series(1, 1000) i;
ANALYZE mdam_single_col_tbl;

EXPLAIN (COSTS OFF)
SELECT * FROM mdam_single_col_tbl WHERE x = 1 OR x = 5;

DROP TABLE mdam_single_col_tbl;

--
-- Prepared statements: MDAM requires constant operands for its rewrite, so
-- generic plans (which use Param nodes instead of Const) must not produce
-- MDAM paths.  Force plan_cache_mode = force_generic_plan so we reliably
-- get the generic plan rather than a custom plan that still has constants.
--
SET plan_cache_mode = force_generic_plan;

PREPARE mdam_prep(int, int, int, int) AS
SELECT * FROM mdam_test_tbl
WHERE (a = $1 AND c = $2) OR (a = $3 AND b = $4);

EXPLAIN (COSTS OFF) EXECUTE mdam_prep(5, 10, 10, 20);

DEALLOCATE mdam_prep;
RESET plan_cache_mode;

--
-- Backward scan: ORDER BY ... DESC should use backward index scan
-- without an explicit Sort node, by reversing disjunct order and
-- scanning each sub-path in BackwardScanDirection.
--
EXPLAIN (COSTS OFF)
SELECT * FROM mdam_test_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20)
ORDER BY a DESC, b DESC, c DESC, d DESC;

SELECT a, b, c, d FROM mdam_test_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20)
ORDER BY a DESC, b DESC, c DESC, d DESC;

--
-- Fuzz-derived: value-bounded inputs whose union spans (-inf, +inf) must not
-- merge to "no scan key on column" when the original predicate excludes NULL.
--
-- For each test below the OR has two arms with bounded constraints on the
-- same column that together cover every non-NULL value (e.g. b < 100 OR
-- b > 50); merging those into an unconstrained interval would emit an Index
-- Scan with no scan key on the column, admitting rows where that column is
-- NULL that the original predicate excludes.  merge_interval_list now splits
-- at the overlap boundary (and step 4c only re-consolidates when a sibling
-- IS NULL path explicitly grants NULL coverage).
--
-- Tables have NULLs in the relevant columns so the bug would manifest as
-- extra rows compared to the seqscan/bitmap baseline.
--
CREATE TABLE mdam_null_bug_tbl (a int, b int, c int);
INSERT INTO mdam_null_bug_tbl
SELECT i % 50,
       CASE WHEN i % 13 = 0 THEN NULL ELSE (i * 7) % 200 END,
       i % 25
FROM generate_series(1, 100000) i;
CREATE INDEX mdam_null_bug_idx ON mdam_null_bug_tbl (a, b, c);
ANALYZE mdam_null_bug_tbl;

--
-- Case 1: two arms with same leading-column key and complementary b ranges
-- that together cover all non-NULL b.  Without the fix, MDAM emitted a
-- single (a = 5) retrieval with no b constraint, admitting rows where
-- b IS NULL.  With the fix, MDAM splits b at the overlap boundary (100).
-- The third arm gives MDAM enough work to win the cost race over a plain
-- index scan with a Filter, so the Append IS chosen.
--
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b < 100) OR (a = 5 AND b > 50) OR (a = 10 AND c = 0);
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a = 5 AND b < 100) OR (a = 5 AND b > 50) OR (a = 10 AND c = 0);
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b < 100) OR (a = 5 AND b > 50) OR (a = 10 AND c = 0);
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a = 5 AND b < 100) OR (a = 5 AND b > 50) OR (a = 10 AND c = 0);

-- Sanity-check: the bug specifically over-counts rows where a = 5 AND b IS
-- NULL.  This count is the inflation that would appear in 'actual' above
-- if the bug returned.
SELECT count(*) AS extras_if_buggy FROM mdam_null_bug_tbl
WHERE a = 5 AND b IS NULL;

--
-- Case 2: step 4c's IS NULL sibling logic.  The first arm is silent on b
-- (so an IS NULL pseudo-interval is generated for b during shattering); the
-- second arm constrains b.  Step 3 merges the silent arm's value-bounded
-- b retrievals back to "no b constraint" via the IS NULL input.  Step 4a
-- then re-shatters that no-b path into (b < 20), (b = 20), (b > 20), and
-- (b IS NULL) sub-paths.  Step 4c's pairwise coalesce would, with only the
-- value sub-paths in view, see the b<=20 + b>20 merge fall into the split
-- branch (no IS NULL among iv1, iv2) -- leaving 4 retrievals instead of 2.
-- The IS NULL sibling lookup fixes this: it pulls the (b IS NULL) path
-- into the merge so coalesce can collapse it back to a single (a = 5)
-- retrieval (matching the pre-bug-fix plan shape for non-nullable data).
--
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a = 5 AND c = 10) OR (a = 10 AND b = 20);

--
-- Case 3: original fuzz-tester reduction.  MDAM may decline this shape on
-- cost (or due to ordering conflicts from the additional split retrievals),
-- but the result must still match -- if the bug returned and MDAM did win
-- the cost race, the count would silently inflate.
--
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a >= 46 AND c >= 8) OR (a >= 56 AND c < 13);
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a >= 46 AND c >= 8) OR (a >= 56 AND c < 13);
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a >= 46 AND c >= 8) OR (a >= 56 AND c < 13);
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a >= 46 AND c >= 8) OR (a >= 56 AND c < 13);

--
-- IS NULL / IS NOT NULL in MDAM OR clauses.
--
-- Reuses mdam_null_bug_tbl: column b has NULLs (i % 13 == 0).  Each case
-- compares an enable_mdam=off baseline (bitmap path) against the MDAM
-- plan.  EXPLAIN is included so a regression in the new NullTest handling
-- shows up as a plan-shape diff in addition to any row-count diff.
--

-- Case A: IS NULL alongside a value predicate in a sibling arm.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NULL) OR (a = 10 AND b > 100);
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NULL) OR (a = 10 AND b > 100);
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NULL) OR (a = 10 AND b > 100);
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NULL) OR (a = 10 AND b > 100);

-- Case B: IS NULL on a leading column, OR'd with an equality.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE b IS NULL OR b = 50;
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE b IS NULL OR b = 50;
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE b IS NULL OR b = 50;
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE b IS NULL OR b = 50;

-- Case C: IS NOT NULL on a non-leading column, only constraint on that
-- column for its arm.  Without an explicit IS NOT NULL scankey the
-- resulting scan would admit b IS NULL rows that arm 1 excluded.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NOT NULL) OR a = 10;
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NOT NULL) OR a = 10;
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NOT NULL) OR a = 10;
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a = 5 AND b IS NOT NULL) OR a = 10;

-- Case D: IS NOT NULL redundant with a value range (within-conjunct).
-- The value range b > 50 already excludes NULLs, so IS NOT NULL drops
-- harmlessly during interval extraction.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (b > 50 AND b IS NOT NULL) OR a = 7;
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (b > 50 AND b IS NOT NULL) OR a = 7;
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (b > 50 AND b IS NOT NULL) OR a = 7;
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (b > 50 AND b IS NOT NULL) OR a = 7;

-- Case E: degenerate "true for every row" -- IS NULL OR IS NOT NULL on
-- the same column.  MDAM may decline this on cost; either plan is fine
-- as long as the count matches.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE b IS NULL OR b IS NOT NULL;
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE b IS NULL OR b IS NOT NULL;
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE b IS NULL OR b IS NOT NULL;
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE b IS NULL OR b IS NOT NULL;

-- Case F: IS NOT NULL combined with an IN list in the other arm.
SET enable_mdam = off;
SET enable_bitmapscan = on;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a IN (1, 2) AND b IS NOT NULL) OR a = 5;
SELECT count(*) AS expect FROM mdam_null_bug_tbl
WHERE (a IN (1, 2) AND b IS NOT NULL) OR a = 5;
SET enable_mdam = on;
SET enable_bitmapscan = off;
EXPLAIN (COSTS OFF)
SELECT count(*) FROM mdam_null_bug_tbl
WHERE (a IN (1, 2) AND b IS NOT NULL) OR a = 5;
SELECT count(*) AS actual FROM mdam_null_bug_tbl
WHERE (a IN (1, 2) AND b IS NOT NULL) OR a = 5;

DROP TABLE mdam_null_bug_tbl;

--
-- MDAM ordering bug: an Append of two index scans both of which can emit
-- a IS NULL rows.  Reduced from mdam_fuzz_test.py.
--
-- Predicate:
--   ((a = 84 AND b >= 137 AND c IN (12, 32, 40))
--    OR b > 92
--    OR (a IS NULL AND b < 80))
--
-- After merging, arm 1 is absorbed into arm 2's b > 92 (137 > 92), leaving
-- two retrievals:
--   R1: b > 92               -- no a constraint, scans NULL a too
--   R2: a IS NULL AND b < 80
--
-- Both R1 and R2 produce rows with a IS NULL: R1 emits them at the end of
-- its index-order traversal (a=NULL last under ASC NULLS LAST), R2 emits
-- only NULL-a rows.  An Append concatenates R1 || R2, so the NULL-a rows
-- from R1 (b > 92) come *before* the NULL-a rows from R2 (b < 80) -- which
-- violates the requested ORDER BY a, b, c, d.  MDAM should either reject
-- this path or arrange the retrievals so the NULL-a partition isn't split
-- across two siblings.
--
CREATE TABLE mdam_order_bug_tbl (a int, b int, c int, d int);
INSERT INTO mdam_order_bug_tbl
SELECT NULLIF((i*7+13) % 100, 0), NULLIF((i*11+3) % 200, 0),
       NULLIF((i*5+17) % 50, 0),  NULLIF((i*3+7) % 25, 0)
FROM generate_series(1, 20000) i;
CREATE INDEX mdam_order_bug_idx ON mdam_order_bug_tbl (a, b, c, d);
ANALYZE mdam_order_bug_tbl;

-- Nudge random_page_cost down so MDAM wins on cost at this row count;
-- under regression defaults the planner otherwise picks a plain Index Only
-- Scan + Filter and the bug doesn't manifest.
SET random_page_cost = 2;
SET enable_indexscan = on;
SET enable_indexonlyscan = on;
SET enable_sort = off;

-- Confirm MDAM picks the Append-of-two-IndexScans plan we care about.
EXPLAIN (COSTS OFF)
SELECT a, b, c, d FROM mdam_order_bug_tbl
WHERE ((a = 84 AND b >= 137 AND c IN (12, 32, 40))
       OR b > 92
       OR (a IS NULL AND b < 80))
ORDER BY a, b, c, d;

-- The boundary lives at position 10601: the first row with a IS NULL.
-- Correctly-ordered output puts (NULL, 54, 22, 5) here (smallest b among
-- NULL-a rows).  Buggy MDAM emits NULL-a rows from R1 (b > 92) first, so
-- this position contains (NULL, 154, 22, 5) instead.
SELECT a, b, c, d FROM mdam_order_bug_tbl
WHERE ((a = 84 AND b >= 137 AND c IN (12, 32, 40))
       OR b > 92
       OR (a IS NULL AND b < 80))
ORDER BY a, b, c, d
LIMIT 1 OFFSET 10600;

DROP TABLE mdam_order_bug_tbl;
RESET enable_sort;
RESET enable_indexscan;
RESET enable_indexonlyscan;
RESET random_page_cost;

--
-- MDAM IN-list range-hull bug: when a top-level AND combines two OR
-- branches that each constrain the same trailing column (here d),
-- shattering must keep the disjoint values as separate retrievals.
-- Instead MDAM emits an Index Cond like (d >= 446 AND d <= 804) for one
-- Append child -- the hull of two disjoint equalities (d = 446) and
-- (d = 804) -- letting rows with d strictly between the two endpoints
-- through.  Reduced from mdam_fuzz_test.py.
--
-- Predicate:
--   (d = 446 OR (a = 4 AND d = 804))
--   AND (b = 7 OR (b <= 16 AND d IN (758, 882)))
--
-- The only row that legitimately matches in the data below is
-- (a, b, c, d) = (2, 7, 29, 446); MDAM additionally returns four
-- spurious rows with a = 4, b = 7, d in {472, 679, 733, 787} (all in
-- the hull [446, 804] but not equal to 446 or 804).
--
CREATE TABLE mdam_inlist_bug_tbl (a int, b int, c int, d int);
INSERT INTO mdam_inlist_bug_tbl
SELECT (i % 9) + 1, (i % 49) + 1, (i % 199) + 1, ((i * 31) % 999) + 1
FROM generate_series(1, 5000) i;
CREATE INDEX mdam_inlist_bug_idx ON mdam_inlist_bug_tbl (a, b, c, d);
ANALYZE mdam_inlist_bug_tbl;

EXPLAIN (COSTS OFF)
SELECT count(*) AS actual FROM mdam_inlist_bug_tbl
WHERE (d = 446 OR (a = 4 AND d = 804))
  AND (b = 7 OR (b <= 16 AND d IN (758, 882)));

SELECT count(*) AS actual FROM mdam_inlist_bug_tbl
WHERE (d = 446 OR (a = 4 AND d = 804))
  AND (b = 7 OR (b <= 16 AND d IN (758, 882)));

SELECT a, b, c, d FROM mdam_inlist_bug_tbl
WHERE (d = 446 OR (a = 4 AND d = 804))
  AND (b = 7 OR (b <= 16 AND d IN (758, 882)))
ORDER BY a, b, c, d;

DROP TABLE mdam_inlist_bug_tbl;

--
-- MDAM crash: x = 1 contradicts x IS NULL, but the combination of an
-- OR-containing-AND with a contradictory IS NULL and a trailing column
-- (y) reference crashes the planner during MDAM processing.
--
CREATE TABLE mdam_crash_mixed (x integer, y text);
CREATE INDEX mdam_crash_mixed_xy ON mdam_crash_mixed USING btree (x, y);

EXPLAIN (COSTS OFF)
SELECT 1 FROM mdam_crash_mixed
WHERE x = 1 AND (x IS NULL AND (y < 'a' OR y IS NOT NULL) OR y = 'q');

DROP TABLE mdam_crash_mixed;

-- Cleanup
RESET enable_seqscan;
RESET enable_bitmapscan;
RESET max_parallel_workers_per_gather;
RESET enable_mdam;

DROP TABLE mdam_test_tbl;
