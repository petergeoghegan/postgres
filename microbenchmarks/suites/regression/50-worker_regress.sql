-- Drop existing table
DROP TABLE IF EXISTS worker_regress CASCADE;

-- Table: worker_regress (5M rows, fillfactor=90)
SELECT setseed(.00003612716763005780);
CREATE TABLE worker_regress (a bigint, b text) WITH (fillfactor = 90, autovacuum_enabled = false);
INSERT INTO worker_regress
SELECT -1 * a, b
FROM (
  SELECT r, a, b, generate_series(0, 2 - 1) AS p
  FROM (
    SELECT
      row_number() OVER () AS r,
      a,
      b
    FROM (
      SELECT
        i AS a,
        md5(i::text) AS b
      FROM
        generate_series(1, 5000000) s(i)
      ORDER BY
        (i + 1024 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 2 + p) + 2 * (random() - 0.5));
CREATE INDEX idx_worker_regress ON worker_regress(a DESC) WITH (deduplicate_items=false);
VACUUM ANALYZE worker_regress;

CHECKPOINT;
