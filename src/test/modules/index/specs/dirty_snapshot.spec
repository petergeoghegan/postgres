# A dirty snapshot sees committed rows plus the in-progress changes of other
# transactions; an MVCC snapshot sees only a consistent committed point in
# time.  Constraint enforcement (unique, foreign key) depends on the former: a
# dirty-snapshot index scan must find a row that a concurrent, not-yet-committed
# transaction is creating, and must stop finding the superseded row the instant
# that transaction commits -- even while an older MVCC snapshot still sees it.
#
# The four permutations cover two axes.  HOT-ness: the writer changes a
# non-indexed column (old and new versions share one index entry via a HOT
# chain) or the indexed column (each version is its own index entry).  Outcome:
# the writer commits or aborts.  The dirty/MVCC visibility result depends only
# on commit-vs-abort, never on HOT-vs-not; w_hotstat reports which path ran.

setup
{
    CREATE EXTENSION test_indexscan;
    CREATE TABLE dirtysnap (val int, note text) WITH (autovacuum_enabled = off);
    CREATE INDEX dirtysnap_idx ON dirtysnap (val);
    INSERT INTO dirtysnap VALUES (10, 'v1');  -- old version at (0,1)
}

teardown
{
    DROP TABLE dirtysnap;
    DROP EXTENSION test_indexscan;
}

# The writer creates a second, as-yet-uncommitted version of the row at (0,2),
# either as a new index entry (w_update_val) or a HOT-chain member
# (w_update_note).  w_hotstat reports the backend's HOT-update count.
session writer
step w_begin       { BEGIN; }
step w_update_val  { UPDATE dirtysnap SET val = 20 WHERE val = 10; }
step w_update_note { UPDATE dirtysnap SET note = 'v2' WHERE val = 10; }
step w_hotstat     { SELECT pg_stat_get_xact_tuples_hot_updated('dirtysnap'::regclass) AS hot_updated; }
step w_commit      { COMMIT; }
step w_rollback    { ROLLBACK; }

# The observer compares both snapshot types from inside one REPEATABLE READ
# transaction, so its MVCC snapshot is frozen at its first scan -- taken while
# the writer is still in progress.
session observer
step o_begin  { BEGIN ISOLATION LEVEL REPEATABLE READ; }
step o_dirty  { SELECT * FROM index_scan_tids('dirtysnap_idx', 'dirty'); }
step o_mvcc   { SELECT * FROM index_scan_tids('dirtysnap_idx', 'mvcc'); }
step o_commit { COMMIT; }

# While the update is uncommitted (all variants): dirty sees BOTH versions,
# mvcc sees only the OLD one.
# After w_commit:   dirty sees only the NEW version; mvcc still the OLD one.
# After w_rollback: the new version is gone and the old one restored, so dirty
#                   and mvcc agree on the OLD version.

# non-HOT + commit: distinct index entries, new version wins
permutation w_begin w_update_val  w_hotstat o_begin o_dirty o_mvcc w_commit   o_dirty o_mvcc o_commit
# non-HOT + abort:  distinct index entries, old version restored
permutation w_begin w_update_val  w_hotstat o_begin o_dirty o_mvcc w_rollback o_dirty o_mvcc o_commit
# HOT + commit:     one index entry / HOT chain, new version wins
permutation w_begin w_update_note w_hotstat o_begin o_dirty o_mvcc w_commit   o_dirty o_mvcc o_commit
# HOT + abort:      one index entry / HOT chain, old version restored
permutation w_begin w_update_note w_hotstat o_begin o_dirty o_mvcc w_rollback o_dirty o_mvcc o_commit
