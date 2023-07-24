-- The shape of the pgsql-performance thread "Query with order by and limit is
-- very slow - wrong index used" (October 2011,
-- https://www.postgresql.org/message-id/flat/15615.1317654751%40sss.pgh.pa.us):
-- a 3.6M-row table with a primary key, a source_id with few distinct values,
-- and an index on (source_id, id).  The query asks for the first 200 rows of
-- one source in id order.  The matching rows are 30% of the table, which is
-- what the statistics say, but they are not spread over the id range as the
-- LIMIT costing assumes: that source was loaded last, so its rows sit at the
-- end of the table.  The planner walks the primary key filtering on source_id
-- (124 s in the thread) instead of scanning the (source_id, id) index, which
-- returns them in order at once; random_page_cost 2.0 and 1.5 did not change
-- that for the poster.  Scaled down to :nrows rows (1M by default).
--
--   SELECT * FROM records WHERE source_id = 'wbc' ORDER BY id LIMIT 200;
\if :{?nrows}
\else
\set nrows 1000000
\endif
SET max_parallel_workers_per_gather = 0;
SET work_mem = '256MB';
DROP TABLE IF EXISTS records;
CREATE TABLE records (id bigint PRIMARY KEY, source_id text NOT NULL,
                      object_id text NOT NULL, payload text);
INSERT INTO records
SELECT i,
       CASE WHEN i > 0.7 * :nrows THEN 'wbc'
            WHEN i % 7 = 0 THEN 'other-' || (i % 67)
            ELSE 'ebuw' END,
       'oai:' || i, repeat('x', 200)
FROM generate_series(1, :nrows) i;
CREATE INDEX source_id_idx ON records (source_id);
CREATE INDEX source_id_id_idx ON records (source_id, id);
VACUUM ANALYZE records;
