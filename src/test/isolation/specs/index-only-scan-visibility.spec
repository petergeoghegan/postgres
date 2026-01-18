# Index-only scan visibility test
#
# Verify that VACUUM doesn't block forever trying to acquire a cleanup lock
# on an index page while an index-only scan has a cursor open.  With MVCC
# scans, we eagerly drop the pin on each batch's leaf page after caching
# visibility info, allowing VACUUM to proceed.
setup
{
	-- Low fillfactor + wide tuples = multiple heap blocks with few rows
	CREATE TABLE ios_vis_test
  (a int NOT NULL,
   b int NOT NULL,
   pad char(1024) DEFAULT '')
	WITH (autovacuum_enabled = false, fillfactor = 10);

	INSERT INTO ios_vis_test SELECT g.i, g.i FROM generate_series(1, 10) g(i);

	CREATE INDEX ios_vis_test_a ON ios_vis_test (a);
}

teardown
{
	DROP TABLE ios_vis_test;
}


session s1

# Force index-only scan:
setup {
	SET enable_bitmapscan = false;
	SET enable_indexonlyscan = true;
	SET enable_indexscan = true;
}

step s1_begin { BEGIN; }
step s1_commit { COMMIT; }

step s1_prepare {
	DECLARE foo NO SCROLL CURSOR FOR SELECT a FROM ios_vis_test WHERE a > 0;
}

step s1_fetch_1 {
	FETCH FROM foo;
}

step s1_fetch_all {
	FETCH ALL FROM foo;
}


session s2

# Keep row 1 so cursor has a row to "rest" on
step s2_delete {
	DELETE FROM ios_vis_test WHERE a > 1;
}

# Disable truncation to avoid unrelated AccessExclusiveLock waits
step s2_vacuum {
	VACUUM (TRUNCATE false) ios_vis_test;
}

permutation
	# VACUUM first, to ensure VM exists
	s2_vacuum

	# Delete nearly all rows
	s2_delete

	# Open a cursor and fetch one row, pinning the first leaf page
	s1_begin
	s1_prepare
	s1_fetch_1

  # This VACUUM must not block forever waiting for a cleanup lock.  It will
  # mark heap pages as all-visible, but the scan has already cached visibility
  # info, so subsequent fetches correctly see no additional rows.
	s2_vacuum

  # Must return no rows (deleted rows are not visible to our MVCC snapshot)
	s1_fetch_all

	s1_commit
