--
-- Cause some overflow insert and splits.
--
drop table if exists hash_split_heap;
CREATE TABLE hash_split_heap (keycol INT);
INSERT INTO hash_split_heap SELECT 1 FROM generate_series(1, 500) a;
CREATE INDEX hash_split_index on hash_split_heap USING HASH (keycol);
INSERT INTO hash_split_heap SELECT 1 FROM generate_series(1, 5000) a;

-- Let's do a backward scan.
BEGIN;
SET enable_seqscan = OFF;
SET enable_bitmapscan = OFF;

DECLARE c CURSOR FOR SELECT * from hash_split_heap WHERE keycol = 1;
MOVE FORWARD ALL FROM c;
CLOSE c;
END;
