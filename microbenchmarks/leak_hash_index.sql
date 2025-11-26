--
-- HASH_INDEX
--

-- directory paths are passed to us in environment variables
drop table if exists hash_i4_heap;
CREATE TABLE hash_i4_heap (
	seqno 		int4,
	random 		int4
);

\set filename '/mnt/nvme/postgresql/patch/source/src/test/regress/data/hash.data'
COPY hash_i4_heap FROM :'filename';

-- the data in this file has a lot of duplicates in the index key
-- fields, leading to long bucket chains and lots of table expansion.
-- this is therefore a stress test of the bucket overflow code (unlike
-- the data in hash.data, which has unique index keys).
--
-- \set filename :abs_srcdir '/data/hashovfl.data'
-- COPY hash_ovfl_heap FROM :'filename';

ANALYZE hash_i4_heap;

CREATE INDEX hash_i4_index ON hash_i4_heap USING hash (random int4_ops);

--
-- hash index
-- grep 843938989 hash.data
--
SELECT * FROM hash_i4_heap
   WHERE hash_i4_heap.random = 843938989;

--
-- hash index
-- grep 66766766 hash.data
--
SELECT * FROM hash_i4_heap
   WHERE hash_i4_heap.random = 66766766;
