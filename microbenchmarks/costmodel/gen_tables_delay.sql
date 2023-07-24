-- Cost model rig tables.
--
-- td: "dense" table, ~49 rows per heap page, 10M rows, heap ordered by id.
-- ts: "sparse" table, exactly 1 row per heap page (fillfactor 10), 200k rows.
--
-- Key columns (each a permutation of 1..N, so BETWEEN lo AND lo+k-1 always
-- selects exactly k rows for every layout):
--   a_corr  = id                   (correlation 1: index order == heap order)
--   a_rand  = random permutation   (correlation ~0: random heap access)
--   a_run4  = index order visits the heap in runs of 4 consecutive pages,
--             runs in random order (correlation ~0, but some I/O combining)
--   a_jit   = id jittered by +-16 heap pages (correlation ~1, local scatter)
--   b_rand  = a second random permutation (two-index tests)
--   c_late  = random permutation whose smallest 10% of values all sit in the
--             last 10% of the heap (LIMIT demonstration)
SET work_mem = '4GB';
SET maintenance_work_mem = '4GB';
-- UNLOGGED tables: a logged table loaded under synchronous_commit=off can
-- have its commit record still unflushed when VACUUM runs, so VACUUM cannot
-- set the XMIN_COMMITTED hint bits and leaves the visibility map unset on
-- the affected pages (a known issue).  Unlogged tables have no WAL to
-- flush, so the VACUUM below sets every all-visible bit deterministically.
SET max_parallel_workers_per_gather = 0;
SET max_parallel_maintenance_workers = 0;
SET synchronize_seqscans = off;

\set ON_ERROR_STOP on
DROP TABLE IF EXISTS td1, td, ts1, ts;

-- td1 has exactly the tuple size of td (7 int4 + 100-byte text), so the
-- ctid block numbers of td1 are the block numbers rows will have in td.
BEGIN;
CREATE UNLOGGED TABLE td1 (id int4 NOT NULL, r1 int4 NOT NULL, r2 int4 NOT NULL,
                  r3 int4 NOT NULL, r4 int4 NOT NULL, d1 int4 NOT NULL,
                  d2 int4 NOT NULL, pad text NOT NULL);
ALTER TABLE td1 ALTER COLUMN pad SET STORAGE PLAIN;
SELECT setseed(0.42);
INSERT INTO td1
SELECT i, (random() * 2147483647)::int4, (random() * 2147483647)::int4,
       (random() * 2147483647)::int4, (random() * 2147483647)::int4, 0, 0,
       repeat('x', 100)
FROM generate_series(1, 5000000) i;
COMMIT;
CHECKPOINT;

BEGIN;
CREATE UNLOGGED TABLE td (id int4 NOT NULL, a_corr int4 NOT NULL, a_rand int4 NOT NULL,
                 a_run4 int4 NOT NULL, a_jit int4 NOT NULL, b_rand int4 NOT NULL,
                 c_late int4 NOT NULL, pad text NOT NULL);
ALTER TABLE td ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO td
SELECT k.id, k.id, k.a_rand, k.a_run4, k.a_jit, k.b_rand, k.c_late, repeat('x', 100)
FROM (
  SELECT id,
         row_number() OVER (ORDER BY r1)::int4 AS a_rand,
         row_number() OVER (ORDER BY hashint4((blk / 4)::int4), id)::int4 AS a_run4,
         row_number() OVER (ORDER BY id + (r2::float8 / 2147483647.0 - 0.5) * 32.0 * rpp)::int4 AS a_jit,
         row_number() OVER (ORDER BY r3)::int4 AS b_rand,
         row_number() OVER (ORDER BY (id <= 0.9 * n), r4)::int4 AS c_late
  FROM (SELECT id, r1, r2, r3, r4, (ctid::text::point)[0]::int4 AS blk FROM td1) s,
       (SELECT count(*) AS n,
               count(*)::float8 / (pg_relation_size('td1') / 8192) AS rpp
        FROM td1) m
) k
ORDER BY k.id;
COMMIT;
DROP TABLE td1;
CHECKPOINT;

-- ts1/ts: one row per page.  Tuple = 24 + 28 + 704 bytes; fillfactor 10
-- leaves 819 bytes per page, so exactly one row fits.
BEGIN;
CREATE UNLOGGED TABLE ts1 (id int4 NOT NULL, r1 int4 NOT NULL, r2 int4 NOT NULL,
                  r3 int4 NOT NULL, r4 int4 NOT NULL, d1 int4 NOT NULL,
                  d2 int4 NOT NULL, pad text NOT NULL) WITH (fillfactor = 10);
ALTER TABLE ts1 ALTER COLUMN pad SET STORAGE PLAIN;
SELECT setseed(0.43);
INSERT INTO ts1
SELECT i, (random() * 2147483647)::int4, (random() * 2147483647)::int4,
       (random() * 2147483647)::int4, (random() * 2147483647)::int4, 0, 0,
       repeat('y', 700)
FROM generate_series(1, 100000) i;
COMMIT;
CHECKPOINT;

BEGIN;
CREATE UNLOGGED TABLE ts (id int4 NOT NULL, a_corr int4 NOT NULL, a_rand int4 NOT NULL,
                 a_run4 int4 NOT NULL, a_jit int4 NOT NULL, b_rand int4 NOT NULL,
                 c_late int4 NOT NULL, pad text NOT NULL) WITH (fillfactor = 10);
ALTER TABLE ts ALTER COLUMN pad SET STORAGE PLAIN;
INSERT INTO ts
SELECT k.id, k.id, k.a_rand, k.a_run4, k.a_jit, k.b_rand, k.c_late, repeat('y', 700)
FROM (
  SELECT id,
         row_number() OVER (ORDER BY r1)::int4 AS a_rand,
         row_number() OVER (ORDER BY hashint4((blk / 4)::int4), id)::int4 AS a_run4,
         row_number() OVER (ORDER BY id + (r2::float8 / 2147483647.0 - 0.5) * 32.0 * rpp)::int4 AS a_jit,
         row_number() OVER (ORDER BY r3)::int4 AS b_rand,
         row_number() OVER (ORDER BY (id <= 0.9 * n), r4)::int4 AS c_late
  FROM (SELECT id, r1, r2, r3, r4, (ctid::text::point)[0]::int4 AS blk FROM ts1) s,
       (SELECT count(*) AS n,
               count(*)::float8 / (pg_relation_size('ts1') / 8192) AS rpp
        FROM ts1) m
) k
ORDER BY k.id;
COMMIT;
DROP TABLE ts1;
CHECKPOINT;

CREATE INDEX td_corr ON td (a_corr);
CREATE INDEX td_rand ON td (a_rand);
CREATE INDEX td_run4 ON td (a_run4);
CREATE INDEX td_jit ON td (a_jit);
CREATE INDEX td_brand ON td (b_rand);
CREATE INDEX td_clate ON td (c_late);
CREATE INDEX ts_corr ON ts (a_corr);
CREATE INDEX ts_rand ON ts (a_rand);
CREATE INDEX ts_run4 ON ts (a_run4);
CREATE INDEX ts_jit ON ts (a_jit);
CREATE INDEX ts_brand ON ts (b_rand);
CREATE INDEX ts_clate ON ts (c_late);

VACUUM (FREEZE, ANALYZE) td, ts;
CHECKPOINT;

SELECT relname, pg_relation_size(oid) / 8192 AS pages, reltuples::bigint
FROM pg_class WHERE relname IN ('td', 'ts', 'td_corr', 'td_rand', 'ts_corr', 'ts_rand')
ORDER BY relname;
SELECT tablename, attname, correlation FROM pg_stats
WHERE tablename IN ('td', 'ts') AND attname LIKE 'a\_%' OR tablename IN ('td','ts') AND attname IN ('b_rand','c_late')
ORDER BY tablename, attname;
-- layout sanity: block number of the first row of each a_run4 run of 4 pages
SELECT a_run4, (ctid::text::point)[0]::int4 AS blk FROM td WHERE a_run4 BETWEEN 1 AND 300 ORDER BY a_run4 LIMIT 300 OFFSET 0 \g /dev/null
