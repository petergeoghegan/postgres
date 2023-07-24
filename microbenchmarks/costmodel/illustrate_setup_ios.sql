-- Index-only scan illustration table.
--
-- t_sparse_ios: two rows per heap page (fillfactor 10, ~380-byte rows), in
-- random page order: a "primary" row (id 1..200000, the rows the queries
-- return) and a "filler" row (id 200001..400000) on the same page.  Deleting
-- a page's filler row and then running VACUUM (INDEX_CLEANUP off) leaves an
-- LP_DEAD stub on the page: the page can no longer be marked all-visible,
-- not by VACUUM (until a VACUUM with index cleanup removes the stub) and not
-- by a scan's opportunistic pruning, so an index-only scan must fetch it.
-- REINDEX drops the deleted rows' index entries.  (Recipe from the
-- prefetch_benchmark.py --ios suite.)  UNLOGGED so VACUUM sets the
-- visibility map right after the load regardless of synchronous_commit.
SET max_parallel_workers_per_gather = 0;
SET maintenance_work_mem = '2GB';
SET work_mem = '2GB';
SELECT setseed(0.5);

DROP TABLE IF EXISTS t_sparse_ios;
CREATE UNLOGGED TABLE t_sparse_ios (id int4 NOT NULL, pad text NOT NULL)
  WITH (fillfactor = 10, autovacuum_enabled = false);
ALTER TABLE t_sparse_ios ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO t_sparse_ios
SELECT id, repeat('y', 340)
FROM (SELECT i AS page, i AS id FROM generate_series(1, 200000) i
      UNION ALL
      SELECT i, i + 200000 FROM generate_series(1, 200000) i) s
ORDER BY hashint4(page), id;         -- primary and filler of a page adjacent
CREATE INDEX t_sparse_ios_id ON t_sparse_ios (id);
VACUUM (FREEZE, ANALYZE) t_sparse_ios;
CHECKPOINT;
SELECT relname, pg_size_pretty(pg_relation_size(oid)) AS size, relpages, relallvisible
FROM pg_class WHERE relname IN ('t_sparse_ios', 't_sparse_ios_id') ORDER BY 1;
SELECT count(*) AS pages_with_2_rows FROM (SELECT (ctid::text::point)[0] AS blk FROM t_sparse_ios GROUP BY 1 HAVING count(*) = 2) s;
