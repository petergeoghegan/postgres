# Test SSI's handling of concurrent insertions into an initially empty
# btree index.
#
# When predicate-locking a completely empty btree there is no page to
# lock, so we lock the whole relation instead.  This was racy: without a
# buffer lock held, a concurrent transaction can insert a matching key
# between the descent that found the index empty and the
# PredicateLockRelation() call.  The scan then misses the inserted tuple,
# but the writer doesn't see the reader's predicate lock either, allowing
# a write skew anomaly to go undetected.

setup
{
  CREATE EXTENSION injection_points;
  CREATE TABLE ssi_btree (id int PRIMARY KEY);
}

teardown
{
  DROP TABLE ssi_btree;
  DROP EXTENSION injection_points;
}

session s1
setup {
  SET debug_parallel_query = off;
  SELECT injection_points_set_local();
  SELECT injection_points_attach('btree-first-empty', 'wait');
  SELECT injection_points_attach('btree-endpoint-empty', 'wait');
}
step s1_begin	{
  BEGIN ISOLATION LEVEL SERIALIZABLE;
  SET LOCAL enable_seqscan = off;
  SET LOCAL enable_bitmapscan = off;
}
# Scan with a useful insertion scan key: descends via _bt_first/_bt_search.
step s1_scan_first        { SELECT id FROM ssi_btree WHERE id = 2; }
# Scan without useful insertion scan keys: starts at _bt_endpoint().
step s1_scan_endpoint     { SELECT id FROM ssi_btree ORDER BY id; }
step s1_insert            { INSERT INTO ssi_btree VALUES (1); }
step s1_commit            { COMMIT; }

session s2
step s2_begin             { BEGIN ISOLATION LEVEL SERIALIZABLE; }
step s2_scan              { SELECT id FROM ssi_btree; }
step s2_insert            { INSERT INTO ssi_btree VALUES (2); }
step s2_commit            { COMMIT; }
step s2_wakeup_first      { SELECT injection_points_wakeup('btree-first-empty'); }
step s2_wakeup_endpoint   { SELECT injection_points_wakeup('btree-endpoint-empty'); }
step s2_detach  {
  SELECT injection_points_detach('btree-first-empty');
  SELECT injection_points_detach('btree-endpoint-empty');
}

# _bt_first()/_bt_search() path
permutation s1_begin
    s2_begin
    s1_scan_first
    s2_scan
    s2_insert
    s2_commit
    s2_wakeup_first
    s1_insert
    s1_commit
    s2_detach

# _bt_endpoint() path
permutation s1_begin
    s2_begin
    s1_scan_endpoint
    s2_scan
    s2_insert
    s2_commit
    s2_wakeup_endpoint
    s1_insert
    s1_commit
    s2_detach
