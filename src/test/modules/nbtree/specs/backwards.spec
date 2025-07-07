# Backwards scan isolation test

setup
{
  CREATE EXTENSION injection_points;
  CREATE TABLE backwards_scan_tbl(col int4) WITH (autovacuum_enabled = off);
  CREATE INDEX ON backwards_scan_tbl(col);
  INSERT INTO backwards_scan_tbl SELECT i FROM generate_series(0, 700) i;
}
setup
{
  VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) backwards_scan_tbl;
}
teardown
{
  DROP EXTENSION injection_points;
  DROP TABLE backwards_scan_tbl;
}

# Wait happens in backwards_scan_session session, wakeup in insert session
#
# The lock-and-validate-new-lastcurrblkno injection point is used for the
# wait.  The lock-and-validate-left injection point is used to generate a
# notification that confirms that we have the desired test coverage.
session backwards_scan_session
setup {
  SELECT injection_points_set_local();
  SELECT injection_points_attach('lock-and-validate-new-lastcurrblkno', 'notice');
  SELECT injection_points_attach('lock-and-validate-left', 'wait');
  SET enable_seqscan=off;
  SET enable_sort=off;
}
step b_scan { SELECT * FROM backwards_scan_tbl WHERE col % 100 = 1 ORDER BY col DESC; }

session insert_scan_session
step i_detach {
  SELECT injection_points_detach('lock-and-validate-left');
  SELECT injection_points_wakeup('lock-and-validate-left');
}
step i_insert { INSERT INTO backwards_scan_tbl SELECT i FROM generate_series(-2000, 700) i; }

# Start a backwards scan session that waits "between pages".  Meanwhile, a
# concurrent session performs insertions that cause many page splits.  When
# the backwards scan session wakes up, it'll have to reason about these
# concurrent page splits the hard way.
permutation b_scan i_insert i_detach
