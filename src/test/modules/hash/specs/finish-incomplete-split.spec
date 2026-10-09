# Test an attempt to finish an incomplete hash bucket split that must give up.
#
# _hash_finish_split walks the new bucket, keeping a pin on its primary page,
# to collect the TIDs that the interrupted split already moved; then it takes
# conditional cleanup locks on both buckets.  When another backend holds a
# pin on the old bucket at that moment it has to give up, and it must release
# the pin it took on the new bucket: the resource owner reports a pin that is
# still held when the statement ends, and a leaked pin also makes every later
# attempt to finish the split fail, since a cleanup lock needs a single pin.
#
# s1 attempts to finish the split and pauses just before the cleanup locks.
# s2 then pins the old bucket's primary page through an open cursor and lets
# s1 continue; s1 gives up and falls back to a plain insert.

setup
{
    CREATE EXTENSION injection_points;
    CREATE TABLE hash_split_test (v int4) WITH (autovacuum_enabled = false);
    INSERT INTO hash_split_test SELECT 1 FROM generate_series(1, 10);
    INSERT INTO hash_split_test VALUES (6);
    ANALYZE hash_split_test;
    CREATE INDEX hash_split_index ON hash_split_test USING hash (v);
}

teardown
{
    DROP TABLE hash_split_test;
    DROP EXTENSION injection_points;
}

session s1
setup
{
    SELECT injection_points_set_local();
    SELECT injection_points_attach('hash-split-before-relocation', 'error');
}
# Leave the first split of bucket 0 incomplete.  v = 1 moves to the new
# bucket 2; v = 6 stays in bucket 0.
step s1_incomplete
{
    INSERT INTO hash_split_test SELECT g FROM generate_series(1000001, 1005000) g;
}
# The notice point guards the scenario: no attempt may finish the split
step s1_rearm
{
    SELECT injection_points_detach('hash-split-before-relocation');
    SELECT injection_points_attach('hash-finish-incomplete-split', 'notice');
    SELECT injection_points_attach('hash-finish-split-before-cleanup-locks', 'wait');
}
# v = 5 hashes to bucket 0, so inserting it tries to finish the split first
step s1_insert
{
    INSERT INTO hash_split_test VALUES (5);
}
step s1_count
{
    SELECT count(*) FROM hash_split_test WHERE v = 5;
}

session s2
setup
{
    SET enable_seqscan = off;
    SET enable_bitmapscan = off;
}
# Pin bucket 0's primary page through the cursor while s1 is paused
step s2_pin
{
    BEGIN;
    DECLARE c CURSOR FOR SELECT v FROM hash_split_test WHERE v = 6;
    FETCH 1 FROM c;
}
step s2_wakeup
{
    SELECT injection_points_detach('hash-finish-split-before-cleanup-locks');
    SELECT injection_points_wakeup('hash-finish-split-before-cleanup-locks');
}
step s2_commit { COMMIT; }

# s1_count runs after s1_insert completes, which keeps the position of the
# completion report stable
permutation s1_incomplete s1_rearm s1_insert s2_pin s2_wakeup s1_count s2_commit
