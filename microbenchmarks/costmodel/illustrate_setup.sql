-- Setup for the plain index scan vs bitmap heap scan illustration.
--
-- Three tables, each 1.6 GB of heap:
--   t_dups   10M rows, 49 rows/page.  64 rows per "grp" value; each group's
--            rows are physically adjacent, groups are laid out in random
--            (hash) order.  Index on grp.  pg_stats correlation ~ 0.
--            (The shape of t_randomized from the PGConf slides, where each
--            key's duplicates sit on adjacent pages.)
--   t_runs   10M rows, 49 rows/page, unique key.  Heap order == key order
--            within runs of 4 pages; the runs are in random order.
--            pg_stats correlation ~ 0.
--   t_sparse 200k rows, one row per page (fillfactor 10), random order.
-- UNLOGGED tables: a logged table loaded under synchronous_commit=off can
-- have its commit record still unflushed when VACUUM runs, so VACUUM cannot
-- set the XMIN_COMMITTED hint bits and leaves the visibility map unset on
-- the affected pages (a known issue).  Unlogged tables have no WAL to
-- flush, so the VACUUM below sets every all-visible bit deterministically.
SET max_parallel_workers_per_gather = 0;
SET maintenance_work_mem = '2GB';
SET work_mem = '2GB';
SELECT setseed(0.5);

DROP TABLE IF EXISTS t_dups, t_runs, t_sparse;

CREATE UNLOGGED TABLE t_dups (grp int4 NOT NULL, id int4 NOT NULL, pad text NOT NULL);
ALTER TABLE t_dups ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO t_dups
SELECT (i - 1) / 64 + 1, i, repeat('x', 108)
FROM generate_series(1, 10000000) i
ORDER BY hashint4((i - 1) / 64), i;
CREATE INDEX t_dups_grp ON t_dups (grp);

CREATE UNLOGGED TABLE t_runs (id int4 NOT NULL, pad text NOT NULL);
ALTER TABLE t_runs ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO t_runs
SELECT i, repeat('x', 112)
FROM generate_series(1, 10000000) i
ORDER BY hashint4((i - 1) / 196), i;      -- 196 rows = 4 pages of 49 rows
CREATE INDEX t_runs_id ON t_runs (id);

CREATE UNLOGGED TABLE t_sparse (id int4 NOT NULL, pad text NOT NULL) WITH (fillfactor = 10);
ALTER TABLE t_sparse ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO t_sparse
SELECT i, repeat('y', 700)
FROM generate_series(1, 200000) i
ORDER BY random();
CREATE INDEX t_sparse_id ON t_sparse (id);

VACUUM (FREEZE, ANALYZE) t_dups, t_runs, t_sparse;
CHECKPOINT;

SELECT relname, pg_size_pretty(pg_relation_size(oid)) AS size, reltuples::bigint AS rows
FROM pg_class WHERE relname IN ('t_dups', 't_runs', 't_sparse', 't_dups_grp', 't_runs_id', 't_sparse_id')
ORDER BY relname;
SELECT tablename, attname, correlation FROM pg_stats
WHERE tablename IN ('t_dups', 't_runs', 't_sparse') AND attname IN ('grp', 'id') ORDER BY 1, 2;
