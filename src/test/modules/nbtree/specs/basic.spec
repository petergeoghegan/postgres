# Basic isolation test for injection points.
#
# This checks the interactions between wakeup, wait and detach.
# Feel free to use it as a template when implementing an isolation
# test with injection points.

setup
{
  CREATE EXTENSION injection_points;
  CREATE TABLE nbtree_incomplete_splits(col int4) WITH (autovacuum_enabled = off);
  CREATE INDEX ON nbtree_incomplete_splits(col);
  INSERT INTO nbtree_incomplete_splits SELECT i FROM generate_series(0, 700) i;
}
setup
{
  VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) nbtree_incomplete_splits;
}
teardown
{
  DROP EXTENSION injection_points;
  DROP TABLE nbtree_incomplete_splits;
}

# Wait happens in the first session, wakeup in the second session.
session backwards_scan_session
setup {
  SELECT injection_points_set_local();
  SELECT injection_points_attach('lock-and-validate-left', 'wait');
  SELECT injection_points_attach('lock-and-validate-new-lastcurrblkno', 'notice');
  SET enable_seqscan=off;
  SET enable_sort=off;
}
step s_noop { }
step b_scan { SELECT * FROM nbtree_incomplete_splits WHERE col % 100 = 1 ORDER BY col DESC; }

session insert_scan_session
setup {
  SELECT injection_points_set_local();
}
step i_noop { }
step i_detach {
  SELECT injection_points_detach('lock-and-validate-left');
  SELECT injection_points_wakeup('lock-and-validate-left');
}
step i_wakeup {
  SELECT injection_points_wakeup('lock-and-validate-left');
}
step i_insert { INSERT INTO nbtree_incomplete_splits SELECT i FROM generate_series(-2000, 200) i; }

# Start a backwards scan session that waits "between pages".  Meanwhile, a
# concurrent session performs insertions that cause many page splits.  When
# the backwards scan session wakes up, it'll have to reason about these
# concurrent page splits.
permutation s_noop b_scan i_noop i_insert i_wakeup i_detach s_noop
