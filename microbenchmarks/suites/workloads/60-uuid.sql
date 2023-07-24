DROP TABLE IF EXISTS t_uuid CASCADE;

-- t_uuid: 5M rows with deterministic UUID primary key.
-- md5(i::text)::uuid produces deterministic values whose sort order
-- is uncorrelated with the sequential insertion order, so
-- pg_stats.correlation ≈ 0.  This maximises random I/O during
-- index scans, which is the ideal scenario for prefetching.
-- Column "val" is a deterministic integer payload so filter quals
-- can be tested, and "payload" adds width to force heap fetches.
CREATE TABLE t_uuid (
    id uuid NOT NULL,
    val integer NOT NULL,
    payload text NOT NULL
);
SELECT setseed(0.5678901234567890);
INSERT INTO t_uuid (id, val, payload)
SELECT md5(i::text)::uuid,
       (random() * 1000)::integer,
       md5((i * 3)::text)
FROM generate_series(1, 5000000) s(i);
ALTER TABLE t_uuid ADD CONSTRAINT t_uuid_pkey PRIMARY KEY (id);
VACUUM (ANALYZE, FREEZE) t_uuid;

CHECKPOINT;
