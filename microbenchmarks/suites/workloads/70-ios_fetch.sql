DROP TABLE IF EXISTS ios_fetch CASCADE;

CREATE UNLOGGED TABLE ios_fetch (a bigint, h bigint, b text)
  WITH (fillfactor = 90, autovacuum_enabled = false, toast.autovacuum_enabled = false);
SELECT setseed(0.00008086035417);
INSERT INTO ios_fetch (a, h, b)
SELECT i, hashint8(i), md5(i::text)
FROM generate_series(1, 10000000) s(i)
ORDER BY (i + 8 * (random() - 0.5));

CREATE INDEX ios_fetch_a_idx ON ios_fetch (a) WITH (deduplicate_items = false);
CREATE INDEX ios_fetch_h_idx ON ios_fetch (h) WITH (deduplicate_items = false);
VACUUM ANALYZE ios_fetch;

DELETE FROM ios_fetch
WHERE (hashint8(a / 95) & 1023)
        < 1024 * 0.85 * LEAST(1.0, GREATEST(0.0,
            (0.5 + 0.32 * cos(2 * pi() * a / 3300000.0)
                 + 0.12 * cos(2 * pi() * a /  410000.0)
                 + 0.06 * cos(2 * pi() * a /   47000.0)
             - 0.40) / 0.60))
  AND (hashint8(a) & 31) = 0;
VACUUM (INDEX_CLEANUP off, ANALYZE) ios_fetch;

REINDEX TABLE ios_fetch;

CHECKPOINT;
