# VACUUM vs concurrent page splits isolation test
#
# btvacuumscan reads the index in physical block order, and holds no locks
# that stop the tree from changing shape underneath it.  Each permutation
# makes VACUUM wait at the nbtree-leave-page-half-dead injection point (hit
# while deleting an empty leaf page) while the concurrent session's inserts
# split a leaf page, then wakes it, forcing VACUUM to take one of its
# recovery paths.  The notice-mode injection points confirm which recovery
# steps ran.
#
# The first permutation covers _bt_unlink_halfdead_page's left sibling
# revalidation: VACUUM remembers the deletion target's left sibling before
# it waits (holding no locks), so it must validate the remembered page's
# right link once it wakes, and step right when a concurrent page split has
# left it pointing to the new page from the split rather than the target.
#
# The second permutation covers btvacuumpage's backtracking: the concurrent
# page split's right half page reuses a block that an earlier VACUUM freed,
# and that the waiting VACUUM's scan already passed, moving tuples that were
# already dead before the split to a block where the scan would otherwise
# miss them.  When the scan reaches the split's left half page (stamped with
# the active VACUUM's cycle ID) and notices that its right link points to an
# earlier block, it backtracks and removes the dead tuples, upholding the
# invariant that index VACUUM always removes every dead TID from the index
# (confirmed by pgstattuple's tuple_count, which counts all leaf page
# tuples, dead or alive).
#
# Note: the delete ranges and expected counts assume the default 8KB BLCKSZ,
# which leaves each leaf page (other than the rightmost) with 366 tuples.
# The bigint index tuples are 16 bytes MAXALIGNed regardless of MAXALIGN, so
# the layout does not vary across platforms.  Leaf key ranges are [0, 365]
# on block 1, [366, 731] on block 2, then [732, 1097], [1098, 1463],
# [1464, 1829] on blocks 4-6, with the remaining tuples on the rightmost
# leaf page, block 7.

setup
{
  CREATE EXTENSION injection_points;
  CREATE EXTENSION pgstattuple;
  CREATE TABLE vacsplit_tbl(col int8) WITH (autovacuum_enabled = off);
  CREATE INDEX vacsplit_idx ON vacsplit_tbl(col) WITH (deduplicate_items = off);
  INSERT INTO vacsplit_tbl SELECT i FROM generate_series(0, 2200) i;
}
teardown
{
  DROP EXTENSION injection_points;
  DROP EXTENSION pgstattuple;
  DROP TABLE vacsplit_tbl;
}

session vacuum_session
setup {
  SET enable_seqscan = off;
  SET enable_sort = off;
}
# Empty the leaf page at block 2, making it the next VACUUM's deletion target
step v_delete_left { DELETE FROM vacsplit_tbl WHERE col BETWEEN 366 AND 731; }
step v_attach_unlink {
  SELECT injection_points_set_local();
  SELECT injection_points_attach('nbtree-leave-page-half-dead', 'wait');
  SELECT injection_points_attach('nbtree-unlink-halfdead-step-right', 'notice');
}
step v_attach_backtrack {
  SELECT injection_points_set_local();
  SELECT injection_points_attach('nbtree-leave-page-half-dead', 'wait');
  SELECT injection_points_attach('nbtree-vacuum-backtrack', 'notice');
}
# The VACUUM that waits while deleting an empty leaf page
step v_vacuum { VACUUM vacsplit_tbl; }
step v_detach_unlink {
  SELECT injection_points_detach('nbtree-unlink-halfdead-step-right');
}
step v_detach_backtrack {
  SELECT injection_points_detach('nbtree-vacuum-backtrack');
}
# Backwards index scan whose search crosses the deleted page's key space.
# Note: calls parallel restricted pg_backend_pid() so that the scan runs in
# the leader process under debug_parallel_query
step v_scan { SELECT col FROM vacsplit_tbl
              WHERE col % 100 = 1 AND pg_backend_pid() <> 0
              ORDER BY col DESC; }
step v_count { SELECT tuple_count FROM pgstattuple('vacsplit_idx'); }
step v_vacuum1 { VACUUM vacsplit_tbl; }
# Assign and complete an XID, so that block 2's deletion is safely behind
# every backend's GlobalVis horizon by the time v_vacuum2 considers placing
# the page in the FSM
step v_advance_xid { INSERT INTO vacsplit_tbl VALUES (2201); }
step v_vacuum2 { VACUUM vacsplit_tbl; }
# Empty the leaf page at block 5 (v_vacuum waits while deleting it), and
# kill the upper key range of the block 6 leaf page (the tuples that
# c_split_reuse's page split moves to the right half page at recycled
# block 2)
step v_delete_mid { DELETE FROM vacsplit_tbl WHERE col BETWEEN 1098 AND 1463 OR col BETWEEN 1700 AND 1829; }

session concurrent_session
step c_split_left { INSERT INTO vacsplit_tbl SELECT 100 FROM generate_series(1, 60) i; }
step c_split_reuse { INSERT INTO vacsplit_tbl SELECT 1500 FROM generate_series(1, 60) i; }
step c_wakeup {
  SELECT injection_points_detach('nbtree-leave-page-half-dead');
  SELECT injection_points_wakeup('nbtree-leave-page-half-dead');
}

# Concurrent split of the page deletion target's left sibling.  VACUUM marks
# the empty block 2 leaf page half-dead, then waits before unlinking it.
# The block 1 leaf page (the left sibling that VACUUM remembered) splits
# during the wait, leaving its right link pointing to the new page from the
# split, so VACUUM must step right just once to relocate the target's true
# left sibling.  The final backwards scan checks the sibling links that the
# unlink step maintained.
permutation v_delete_left
    v_attach_unlink
    v_vacuum
    c_split_left
    c_wakeup
    v_detach_unlink
    v_scan

# Concurrent page split whose right half page reuses a block that the VACUUM
# scan already passed.  An earlier VACUUM pair deletes the empty block 2
# leaf page and places it in the FSM.  The third VACUUM waits while deleting
# the empty block 5 leaf page, and the block 6 leaf page splits during the
# wait: the split's right half reuses block 2, moving the dead tuples in
# block 6's upper key range to a block that the scan already passed.  Once
# woken, VACUUM backtracks to the reused block when it reaches the split's
# left half, and removes the dead tuples that the split moved there.  The
# final tuple_count matches the table's live row count: VACUUM removed every
# dead TID from the index.
permutation v_delete_left
    v_vacuum1
    v_advance_xid
    v_vacuum2
    v_delete_mid
    v_attach_backtrack
    v_vacuum
    c_split_reuse
    c_wakeup
    v_detach_backtrack
    v_count
