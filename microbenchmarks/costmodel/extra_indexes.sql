-- Extra layouts added after the first grid pass:
--   td_dup64: expression index on hashint4((id-1)/64): 64 duplicate keys per
--             group, each group = 64 physically adjacent rows (~1.3 pages),
--             groups in random (hash) order -- the t_randomized shape.
--   ts_run16: 16-page runs in random order on the sparse table (16 rows of
--             one row per page): the plain scan can combine up to
--             io_combine_limit, like the bitmap scan.
SET maintenance_work_mem = '2GB';
CREATE INDEX td_dup64 ON td (hashint4((id - 1) / 64));
CREATE INDEX ts_run16 ON ts (hashint4((id - 1) / 16), id);
ANALYZE td, ts;
SELECT c.relname, pg_relation_size(c.oid) / 8192 AS pages FROM pg_class c WHERE c.relname IN ('td_dup64', 'ts_run16');
SELECT tablename, attname, correlation, n_distinct FROM pg_stats WHERE tablename IN ('td_dup64', 'ts_run16');
