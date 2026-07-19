# Backwards scan isolation test
#
# Test the concurrency rules used by backwards scans, which step left to the
# current page's left sibling using a search that starts from its saved right
# sibling (the page the scan just read).  Concurrent page splits and page
# deletions can leave the scan's saved left link pointing to a page that is no
# longer the correct one for the scan to read next, which the scan must
# recover from.
#
# The lock-and-validate-left injection point is used to make the scan wait
# "between pages", while a concurrent session performs page splits and/or
# page deletions.  The other injection points generate notifications that
# confirm that we have the desired test coverage:
#
# lock-and-validate-step-right fires each time the scan's search steps right
# to recover from a concurrent page split (never more than 4 times per
# search attempt).
#
# lock-and-validate-new-lastcurrblkno fires when the search gives up on the
# scan's saved left link/right sibling pages, and starts over using the
# right sibling page's current left link.
#
# lock-and-validate-lastcurr-deleted fires when the search finds that the
# page that the scan just read was itself concurrently deleted.
#
# Note: the permutations' expected notifications (and the leaf pages that
# each concurrent session step splits or deletes) assume the default 8KB
# BLCKSZ.

setup
{
  CREATE EXTENSION injection_points;
  CREATE TABLE backwards_scan_tbl(col int4) WITH (autovacuum_enabled = off);
  CREATE INDEX ON backwards_scan_tbl(col) WITH (deduplicate_items = off);
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

session backwards_scan_session
setup {
  SELECT injection_points_set_local();
  SET enable_seqscan=off;
  SET enable_sort=off;
  SET debug_parallel_query=off;
}
step b_attach {
  SELECT injection_points_attach('lock-and-validate-left', 'wait');
  SELECT injection_points_attach('lock-and-validate-step-right', 'notice');
  SELECT injection_points_attach('lock-and-validate-new-lastcurrblkno', 'notice');
  SELECT injection_points_attach('lock-and-validate-lastcurr-deleted', 'notice');
}
# Variant that doesn't attach to lock-and-validate-step-right, for
# permutations whose number of step right attempts varies with the amount of
# free space that index tuples' varying alignment padding leaves on each page
step b_attach_nosr {
  SELECT injection_points_attach('lock-and-validate-left', 'wait');
  SELECT injection_points_attach('lock-and-validate-new-lastcurrblkno', 'notice');
  SELECT injection_points_attach('lock-and-validate-lastcurr-deleted', 'notice');
}
step b_scan { SELECT col FROM backwards_scan_tbl
              WHERE col % 100 = 1 ORDER BY col DESC; }
step b_scan_999 { SELECT col FROM backwards_scan_tbl
                  WHERE col <= 999 AND col % 100 = 1 ORDER BY col DESC; }
step b_detach {
  SELECT injection_points_detach('lock-and-validate-step-right');
  SELECT injection_points_detach('lock-and-validate-new-lastcurrblkno');
  SELECT injection_points_detach('lock-and-validate-lastcurr-deleted');
}
step b_detach_nosr {
  SELECT injection_points_detach('lock-and-validate-new-lastcurrblkno');
  SELECT injection_points_detach('lock-and-validate-lastcurr-deleted');
}

session concurrent_session
step i_insert { INSERT INTO backwards_scan_tbl SELECT i FROM generate_series(-2000, 700) i; }
step i_insert_dups { INSERT INTO backwards_scan_tbl SELECT 100 FROM generate_series(1, 60); }
step i_grow { INSERT INTO backwards_scan_tbl SELECT i FROM generate_series(701, 2200) i; }
step d_delete_left { DELETE FROM backwards_scan_tbl WHERE col < 601; }
step d_delete_mid { DELETE FROM backwards_scan_tbl WHERE col BETWEEN 367 AND 2100; }
step vacuum_tbl { VACUUM backwards_scan_tbl; }
step i_detach {
  SELECT injection_points_detach('lock-and-validate-left');
  SELECT injection_points_wakeup('lock-and-validate-left');
}

# Many concurrent page splits.  When the backwards scan session wakes up, its
# search steps right the maximum number of times before giving up and
# starting over with the right sibling page's current left link.
permutation b_attach b_scan i_insert i_detach b_detach

# A single concurrent page split.  When the backwards scan session wakes up,
# its search recovers by stepping right just once.
permutation b_attach b_scan i_insert_dups i_detach b_detach

# Concurrent deletion of all pages to the left of the page that the scan just
# read.  When the backwards scan session wakes up, its search determines that
# the scan has no page to the left to move to, ending the scan.
permutation b_attach d_delete_left b_scan vacuum_tbl i_detach b_detach

# Concurrent deletion of the page that the scan just read (which the scan can
# only safely rely on when a search locates its left sibling using its saved
# right link, which the deleted page's right sibling has acquired).  The scan
# just read a page whose tuples all pointed to dead-to-all heap tuples, which
# VACUUM deletes during the scan's wait, along with all nearby pages.
permutation b_attach_nosr i_grow d_delete_mid b_scan_999 vacuum_tbl i_detach b_detach_nosr
