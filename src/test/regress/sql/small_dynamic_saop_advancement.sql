--
-- HASH_INDEX
--

-- -- directory paths are passed to us in environment variables
-- set client_min_messages=error;
-- drop table if exists hash_i4_heap;
-- reset client_min_messages;
--
-- CREATE TABLE hash_i4_heap (
-- 	seqno 		int4,
-- 	random 		int4
-- );
--
-- \set filename '/mnt/nvme/postgresql/patch/source/src/test/regress/data/hash.data'
-- COPY hash_i4_heap FROM :'filename';
--
-- -- the data in this file has a lot of duplicates in the index key
-- -- fields, leading to long bucket chains and lots of table expansion.
-- -- this is therefore a stress test of the bucket overflow code (unlike
-- -- the data in hash.data, which has unique index keys).
-- --
-- -- \set filename :abs_srcdir '/data/hashovfl.data'
-- -- COPY hash_ovfl_heap FROM :'filename';
--
-- ANALYZE hash_i4_heap;
--
-- CREATE INDEX hash_i4_index ON hash_i4_heap USING hash (random int4_ops);
--
-- SELECT * FROM hash_i4_heap
--    WHERE hash_i4_heap.random = 843938989;
-- EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
-- SELECT * FROM hash_i4_heap
--    WHERE hash_i4_heap.random = 843938989;
--
-- --
-- -- leak
-- --
-- SELECT * FROM hash_i4_heap
--    WHERE hash_i4_heap.random = 66766766;
-- EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
-- SELECT * FROM hash_i4_heap
--    WHERE hash_i4_heap.random = 66766766;

--
-- doublefree.
--
set client_min_messages=error;
drop table if exists hash_split_heap;
reset client_min_messages;
CREATE TABLE hash_split_heap (keycol INT);
INSERT INTO hash_split_heap SELECT 1 FROM generate_series(1, 500) a;
CREATE INDEX hash_split_index on hash_split_heap USING HASH (keycol);
INSERT INTO hash_split_heap SELECT 1 FROM generate_series(1, 5000) a;

-- Let's do a backward scan.
BEGIN;
SET enable_seqscan = OFF;
SET enable_bitmapscan = OFF;

DECLARE c CURSOR FOR SELECT * from hash_split_heap WHERE keycol = 1;
MOVE FORWARD 408 FROM c;
CLOSE c;
END;
