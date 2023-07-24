DROP TABLE IF EXISTS t_hash CASCADE;

-- t_hash: 10M rows exercising hash-index equality scans.  Hash indexes only
-- answer equality, so the prefetching-relevant variable is how many heap rows a
-- single key matches and how scattered they are.  Both indexed columns are
-- assigned with random(), so the rows matching any one key are spread uniformly
-- across the heap (pg_stats.correlation ≈ 0) -- the natural case for data
-- inserted over time -- making the heap fetches random I/O, exactly what index
-- prefetching targets.  A low fillfactor spreads rows over more heap pages to
-- amplify that I/O.
--   * grp: ~5k distinct values (~2k rows each), the large-match key (HA1/HA3).
--   * mid: ~100k distinct values (~100 rows each), the medium-match key (HA2).
--   * val: a deterministic integer payload for filter quals (HA3).
--   * payload: md5 text to add width and force heap fetches.
CREATE TABLE t_hash (
    id      bigint  NOT NULL,
    grp     integer NOT NULL,
    mid     integer NOT NULL,
    val     integer NOT NULL,
    payload text    NOT NULL
) WITH (fillfactor = 20);
SELECT setseed(0.2468013579246801);
INSERT INTO t_hash (id, grp, mid, val, payload)
SELECT i,
       (random() * 5000)::integer,
       (random() * 100000)::integer,
       (random() * 1000)::integer,
       md5(i::text)
FROM generate_series(1, 10000000) s(i);
CREATE INDEX idx_hash_grp ON t_hash USING hash (grp);
CREATE INDEX idx_hash_mid ON t_hash USING hash (mid);
VACUUM (ANALYZE, FREEZE) t_hash;

CHECKPOINT;
