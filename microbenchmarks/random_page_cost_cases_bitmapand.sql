-- Vitaliy Garnashevich's reproduction from the pgsql-hackers thread "Bitmap
-- scan is undercosted?" (December 2017,
-- https://www.postgrespro.com/list/thread-id/2358338): two single-column
-- indexes whose columns are not independent (every row with num = 1 has
-- flag = true), so the planner expects the BitmapAnd to cut the heap rows
-- tenfold and costs the bitmap plan at half the plain index scan, which runs
-- about 2.7x faster with everything cached.  The num = 1 rows are every
-- 100th row, i.e. in heap order, two or three per page.  10M rows by default
-- (:nrows), about 350 MB.
--
--   SELECT count(*) FROM aaa WHERE num = 1 AND flag = true;
\if :{?nrows}
\else
\set nrows 10000000
\endif
SET max_parallel_workers_per_gather = 0;
SET work_mem = '256MB';
DROP TABLE IF EXISTS aaa;
CREATE TABLE aaa AS
SELECT (id % 100)::int AS num, (id % 10 = 1)::bool AS flag
FROM generate_series(1, :nrows) id;
CREATE INDEX i1 ON aaa (num);
CREATE INDEX i2 ON aaa (flag);
VACUUM ANALYZE aaa;
