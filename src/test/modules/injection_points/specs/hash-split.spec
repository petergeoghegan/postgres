# Test hash index scans that run while a bucket split is in progress.
#
# The interesting window opens once the split has relocated tuples to the
# new bucket, before the buckets' split-in-progress flags are cleared.  A
# concurrent scan whose value belongs to the new bucket must visit both
# buckets of the pair: it skips the moved-by-split tuples in the new
# bucket, so it has to read their authoritative copies from the old
# bucket, plus any tuples inserted into the new bucket after the split
# began.
#
# s1 performs the split, pausing inside that window on a wait injection
# point (placed where the splitter holds no buffer content locks).  s2
# then inserts a matching row (which goes to the new bucket) and scans,
# both forwards and backwards.

setup
{
    CREATE EXTENSION injection_points;
    CREATE TABLE hash_split_test (v int4) WITH (autovacuum_enabled = false);
    CREATE INDEX hash_split_index ON hash_split_test USING hash (v);
    CREATE TABLE hash_split_geom AS
      SELECT nbuckets,
             (current_setting('block_size')::bigint / 16) * nbuckets AS fillrows
        FROM (SELECT pg_relation_size('hash_split_index') /
                     current_setting('block_size')::bigint - 2 AS nbuckets) g;
    CREATE TABLE hash_split_key AS
      SELECT min(v)::int4 AS k
        FROM generate_series(1, 10000) v, hash_split_geom
       WHERE (hashint4(v::int4) & (2 * nbuckets - 1)) = nbuckets;
    INSERT INTO hash_split_test
      SELECT k FROM hash_split_key, generate_series(1, 10);
}

teardown
{
    DROP TABLE hash_split_test, hash_split_geom, hash_split_key;
    DROP EXTENSION injection_points;
}

session s1
setup
{
    SELECT injection_points_set_local();
    SELECT injection_points_attach('hash-split-after-relocation', 'wait');
}
# fillrows is guaranteed to cross the split threshold of ffactor * nbuckets
# tuples: a hash index entry occupies at least 16 bytes (line pointer
# included), and the default fillfactor targets 75% page fullness, so
# ffactor can never exceed block_size / 16 tuples per bucket
step s1_split
{
    INSERT INTO hash_split_test
      SELECT 1000000 + g FROM hash_split_geom, generate_series(1, fillrows) g;
}
step s1_noop { }

session s2
setup
{
    SET enable_seqscan = off;
    SET enable_bitmapscan = off;
}
step s2_insert
{
    INSERT INTO hash_split_test SELECT k FROM hash_split_key;
}
step s2_scan
{
    SELECT count(*) FROM hash_split_test
     WHERE v = (SELECT k FROM hash_split_key);
}
step s2_back
{
    BEGIN;
    DECLARE c SCROLL CURSOR FOR
      SELECT v = (SELECT k FROM hash_split_key) FROM hash_split_test
       WHERE v = (SELECT k FROM hash_split_key);
    MOVE FORWARD ALL IN c;
    FETCH BACKWARD ALL FROM c;
    COMMIT;
}
step s2_detach { SELECT injection_points_detach('hash-split-after-relocation'); }
step s2_wakeup { SELECT injection_points_wakeup('hash-split-after-relocation'); }

# Scan during the paused split, then let the split finish and scan again.
# The detach must happen before the wakeup: the same INSERT can trigger
# another split, which must not hit the injection point again.  The no-op
# step in the splitter's session forces the isolation tester to wait for
# s1_split to finish, keeping the position of its completion report stable
# regardless of how long the remaining inserts take (see basic.spec).
permutation s1_split s2_insert s2_scan s2_back s2_detach s2_wakeup s1_noop s2_scan
