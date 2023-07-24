DROP TABLE IF EXISTS t_spgist CASCADE;

-- t_spgist: 10M rows exercising SP-GiST's two canonical workloads in one table.
-- Both indexed columns are uncorrelated with heap (insertion) order -- the
-- natural situation for data inserted over time rather than spatially or
-- lexically sorted -- so region, nearest-neighbor, and range scans touch heap
-- pages in scattered order (random I/O), exactly what index prefetching
-- targets.  A low fillfactor spreads rows over more heap pages to amplify it.
--
--   * location: random geographic-style points, indexed by SP-GiST
--     quad_point_ops (the default point opclass).  Exercised by the region
--     (<@) and nearest-neighbor (<->) scans.
--   * tok: random 32-char tokens (md5 text), indexed by SP-GiST text_ops (a
--     suffix tree).  Exercised by a lexical range scan.  The column uses the C
--     collation so the range bounds select a predictable fraction of the table.
CREATE TABLE t_spgist (
    id       bigint NOT NULL,
    location point  NOT NULL,
    tok      text   COLLATE "C" NOT NULL,
    payload  text   NOT NULL
) WITH (fillfactor = 20);
SELECT setseed(0.1234567890123456);
INSERT INTO t_spgist (id, location, tok, payload)
SELECT i,
       point(random() * 1000.0, random() * 1000.0),
       md5(i::text),
       md5((i + 1)::text)
FROM generate_series(1, 10000000) s(i);
CREATE INDEX idx_spgist_point ON t_spgist USING spgist (location);
CREATE INDEX idx_spgist_tok   ON t_spgist USING spgist (tok);
VACUUM (ANALYZE, FREEZE) t_spgist;

CHECKPOINT;
