-- cursor_reversal_pages.sql
--
-- A scrollable cursor over a btree array-key scan that reverses direction
-- right at the start of the scan.  The comments describe which index pages
-- each FETCH reads on master (825b9439^) and with the amgetbatch commit
-- (825b9439), measured with pg_buffercache usage counts on a freshly built
-- table (see the end of this file).
--
-- Index pages for this table, as built below (bulk load, default fillfactor):
--   0   metapage (read once per backend, then cached in rd_amcache)
--   3   root
--   7   leaf holding (5, 5)
--   6   left sibling of 7
--   8   right sibling of 7 (rest of a = 5 and the start of a = 6)
--   9   right sibling of 8
--   27  leaf holding (20, 5)
--
-- Run with: psql -X -f cursor_reversal_pages.sql

CREATE TABLE revarr (a int, b int, pad text);
INSERT INTO revarr SELECT a, b, repeat('y', 60)
  FROM generate_series(1, 20) a, generate_series(1, 500) b;
CREATE INDEX revarr_a_b ON revarr (a, b);
VACUUM (ANALYZE) revarr;

-- Plain index scan (not index-only), so that the scan reads the heap too
SET enable_seqscan = off;
SET enable_bitmapscan = off;
SET enable_indexonlyscan = off;
SET enable_sort = off;

EXPLAIN (COSTS OFF)
SELECT a, b FROM revarr WHERE a IN (5, 20) AND b = 5 ORDER BY a, b;

BEGIN;

DECLARE c SCROLL CURSOR FOR
  SELECT a, b FROM revarr WHERE a IN (5, 20) AND b = 5 ORDER BY a, b;

-- Step 1: first row, (5, 5).
--
--   master:  0, 3, 7   (metapage on first use, root, leaf)
--   patch:   0, 3, 7
--
-- Both descend to leaf 7 and read it.  Its high key makes _bt_checkkeys
-- advance the array to 20 and schedule a primitive index scan for it:
-- needPrimScan = true, moreRight = false.  moreLeft is false because this is
-- the first page of the scan's first primitive scan.
FETCH 1 FROM c;

-- Step 2: reverse at the very start.  Returns nothing.
--
--   master:  (no page read)
--   patch:   6
--
-- master: _bt_steppage sees the direction change and cancels the scheduled
-- primitive scan.  _bt_readnextpage then sees moreLeft = false and ends the
-- scan without reading anything.  It also invalidates currPos, so the scan
-- has forgotten leaf 7 entirely.
--
-- patch: the table AM flips the batch's direction and calls btposreset,
-- which resets the array keys to their backward start position and forces
-- moreLeft = true.  btgetbatch had not set knownEndBackward on the batch,
-- because it withholds both knownEnd flags whenever needPrimScan is set
-- (only the forward flag was really affected).  So _bt_next steps left to
-- leaf 6, whose tuples (a = 4) exhaust the arrays in the backward direction,
-- and the scan ends.  The batch for leaf 7 stays loaded.
FETCH BACKWARD 1 FROM c;

-- Step 3: forward again.  Returns (5, 5) a second time.
--
--   master:  3, 7
--   patch:   (no page read)
--
-- master: currPos is invalid, so btgettuple calls _bt_first again: a fresh
-- descent through the root to leaf 7.  Leaf 7's high key schedules the
-- primitive scan for a = 20 again.
--
-- patch: the row comes straight out of the still-loaded batch for leaf 7.
FETCH 1 FROM c;

-- Step 4: second row, (20, 5).
--
--   master:  3, 27
--   patch:   8, 9, 3, 27
--
-- master: _bt_steppage sees moreRight = false, and _bt_start_prim_scan
-- starts the scheduled primitive scan: root, then leaf 27.
--
-- patch: stepping off the batch is another direction change, so btposreset
-- runs again: the arrays go back to their forward start (a = 5),
-- needPrimScan is cleared and moreRight is forced to true.  _bt_next
-- therefore walks right from leaf 7: leaf 8 advances the arrays to 20 and
-- the scan-behind heuristic reads one more page (leaf 9) before giving up
-- and scheduling the primitive scan, which then descends through the root
-- to leaf 27.
FETCH 1 FROM c;

-- Step 5: past the end.  Returns nothing.
--
--   master:  (no page read)
--   patch:   (no page read)
--
-- Leaf 27 exhausted the arrays, so both know the scan is over in this
-- direction without reading anything.
FETCH 1 FROM c;

COMMIT;

-- Totals for the steps above, excluding the metapage:
--
--   steps 1-2 (reverse at the start):          master 2, patch 3
--   steps 1-3 (reverse, then come back):       master 4, patch 3
--   steps 1-4 (reverse, then read everything): master 6, patch 7
--
-- Reversing at the end of the scan instead (FETCH ALL, FETCH BACKWARD ALL,
-- FETCH ALL) reads the same 8 pages on both.  With a IN (1, 20) leaf 7 has
-- no left sibling and step 2 reads nothing on either.

DROP TABLE revarr;

-- How the page numbers were measured: on a fresh copy of the table, snapshot
-- pg_buffercache usage counts for the index before and after the cursor
-- steps; every page a step touches gains one usage count (the counts start
-- well below the cap of 5 on a freshly built index that fits in
-- shared_buffers).
--
--   CREATE EXTENSION pg_buffercache;
--   SELECT relblocknumber, usagecount FROM pg_buffercache
--   WHERE relfilenode = pg_relation_filenode('revarr_a_b')
--   ORDER BY relblocknumber;
