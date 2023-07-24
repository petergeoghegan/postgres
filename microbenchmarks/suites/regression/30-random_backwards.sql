-- Drop existing tables
DROP TABLE IF EXISTS t CASCADE;
DROP TABLE IF EXISTS t_randomized CASCADE;

SET synchronize_seqscans = off;

-- Table 1: t (sequential layout, 312500 x 32 = 10M rows)
CREATE TABLE t (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.3456789012345678);
INSERT INTO t
SELECT a, b
FROM (SELECT
    r,
    a,
    b,
    generate_series(0, 32 - 1) AS p
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
        generate_series(1, 312500) s(i)
      ORDER BY
        (i + 1 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 32 + p) + 8 * (random() - 0.5));
CREATE INDEX t_pk ON t(a ASC) WITH (deduplicate_items=off);

-- Table 2: t_randomized (clustered by hash for random physical layout)
CREATE TABLE t_randomized (a bigint, b text) WITH (fillfactor = 20);
SELECT setseed(0.4567890123456789);
INSERT INTO t_randomized
SELECT a, b
FROM (SELECT
    r,
    a,
    b,
    generate_series(0, 32 - 1) AS p
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
        generate_series(1, 312500) s(i)
      ORDER BY
        (i + 1 * (random() - 0.5))) foo) bar) baz
ORDER BY ((r * 32 + p) + 8 * (random() - 0.5));
CREATE INDEX t_randomized_pk ON t_randomized(a ASC) WITH (deduplicate_items=off);
CREATE INDEX randomizer ON t_randomized (hashint8(a));
CLUSTER t_randomized USING randomizer;

-- Scope to this suite's own tables: an unqualified VACUUM FREEZE / ANALYZE
-- would hit every table in the shared database, e.g. removing the LP_DEAD
-- poison the ios_fetch suite deliberately keeps.
VACUUM (FREEZE) t, t_randomized;
ANALYZE t, t_randomized;
CHECKPOINT;
