DROP TABLE IF EXISTS t_tupdistance_new_regress CASCADE;

-- t_tupdistance_new_regress (2.5M rows x 4 = 10M rows)
CREATE TABLE t_tupdistance_new_regress (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.2345678901234567);
INSERT INTO t_tupdistance_new_regress
SELECT 1 * a, b
FROM (
  SELECT
    r,
    a,
    b,
    generate_series(0, 4 - 1) AS p
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
        (i + 0 * (random() - 0.5))) foo) bar) baz
ORDER BY
  ((r * 4 + p) + 8 * (random() - 0.5));
CREATE INDEX t_tupdistance_new_regress_idx ON t_tupdistance_new_regress(a DESC) WITH (deduplicate_items = false);
VACUUM ANALYZE t_tupdistance_new_regress;

CHECKPOINT;
