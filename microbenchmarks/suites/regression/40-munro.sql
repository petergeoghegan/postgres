-- Drop existing table
DROP TABLE IF EXISTS t_munro CASCADE;

-- Table: t_munro (2.5M rows x 4 = 10M rows, fillfactor=90)
CREATE TABLE t_munro (a bigint, b text) WITH (fillfactor = 90, autovacuum_enabled = false);
SELECT setseed(0.6789012345678901);
INSERT INTO t_munro
SELECT 1 * a, b
FROM (
  SELECT r, a, b, generate_series(0, 4 - 1) AS p
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
        generate_series(1, 2500000) s(i)
      ORDER BY
        (i + 16384 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 4 + p) + 32 * (random() - 0.5));
CREATE INDEX idx_munro ON t_munro(a ASC) WITH (deduplicate_items=false);
VACUUM ANALYZE t_munro;

CHECKPOINT;
