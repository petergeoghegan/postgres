-- The shape of the query in
-- https://www.shayon.dev/post/2024/55/100x-faster-query-in-aurora-postgres-with-a-lower-random_page_cost/
--
-- table_1 has 16 rows.  table_2's rows belong mostly to table_1 ids 150 and
-- 250, interleaved in id order.  Every row is loaded incomplete
-- (completed_at IS NULL) and the table is analyzed; then the rows of ids 150
-- and 250 complete except one in 300, and no ANALYZE runs again: the queue
-- moved on, the statistics did not, so the planner still believes that
-- nearly every row of those ids matches the query below.
--
-- The query, the post's: for each selected table_1 row, 500 of its incomplete
-- table_2 rows in id order.  With the stale statistics the stock planner
-- walks table_2's primary key and filters (it expects a match every other
-- row); the composite partial index returns the rows already in order.
--
--   SELECT t1.*, t2.*
--   FROM table_1 t1
--     CROSS JOIN LATERAL (SELECT * FROM table_2
--                         WHERE table_2.table_1_id = t1.id
--                           AND table_2.completed_at IS NULL
--                         ORDER BY table_2.id LIMIT 500) t2
--   WHERE t1.id = ANY('{150,250}');
\if :{?nrows}
\else
\set nrows 2000000
\endif
SET max_parallel_workers_per_gather = 0;
SET work_mem = '256MB';
DROP TABLE IF EXISTS table_1, table_2;
CREATE TABLE table_1 (id int PRIMARY KEY, name text);
INSERT INTO table_1 SELECT i, 'name ' || i
FROM unnest(ARRAY[10,20,30,40,50,60,70,80,90,100,110,120,130,140,150,250]) i;
CREATE TABLE table_2 (id bigint PRIMARY KEY, table_1_id int NOT NULL,
                      completed_at timestamptz, payload text);
INSERT INTO table_2
SELECT i,
       CASE WHEN i % 10 < 9 THEN (CASE WHEN i % 2 = 0 THEN 150 ELSE 250 END)
            ELSE (ARRAY[10,20,30,40,50,60,70,80,90,100,110,120,130,140])[1 + (i / 10) % 14] END,
       NULL, repeat('x', 80)
FROM generate_series(1, :nrows) i;
CREATE INDEX idx_complete_table_2_on_table_1_id_id_where_completed_at_is_null
  ON table_2 (table_1_id, id) WHERE completed_at IS NULL;
VACUUM ANALYZE table_1, table_2;
UPDATE table_2 SET completed_at = now()
WHERE table_1_id IN (150, 250) AND (id / 2) % 300 <> 0;
VACUUM table_2;
