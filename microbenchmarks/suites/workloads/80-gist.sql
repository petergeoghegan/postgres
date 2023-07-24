DROP TABLE IF EXISTS t_gist_points CASCADE;

-- t_gist_points: 10M geographic-style points (think a "places"/"events" table
-- with a location), indexed by a GiST point_ops index.  Spatial proximity is
-- uncorrelated with heap (insertion) order -- the natural situation for data
-- inserted over time rather than spatially sorted -- so region and
-- nearest-neighbor scans touch heap pages in scattered order (random I/O),
-- exactly what index prefetching targets.  A low fillfactor spreads rows over
-- more heap pages to amplify that I/O.
CREATE TABLE t_gist_points (
    id       bigint NOT NULL,
    location point  NOT NULL,
    payload  text   NOT NULL
) WITH (fillfactor = 20);
SELECT setseed(0.4567890123456789);
INSERT INTO t_gist_points (id, location, payload)
SELECT i,
       point(random() * 1000.0, random() * 1000.0),
       md5(i::text)
FROM generate_series(1, 10000000) s(i);
CREATE INDEX idx_gist_points ON t_gist_points USING gist (location);
VACUUM (ANALYZE, FREEZE) t_gist_points;

CHECKPOINT;
