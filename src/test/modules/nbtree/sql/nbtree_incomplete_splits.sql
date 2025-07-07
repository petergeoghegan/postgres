-- This uses injection points to cause errors that leave some page
-- splits in "incomplete" state
create extension injection_points;

-- Make all injection points local to this process, for concurrency.
SELECT injection_points_set_local();

-- Use the index for all the queries
set enable_seqscan=off;

--
-- First create the test table and some helper functions
--
create table nbtree_incomplete_splits(col int4) with (autovacuum_enabled = off);
create index on nbtree_incomplete_splits(col);
insert into nbtree_incomplete_splits select i from generate_series(0, 10_000) i;

-- nbtree-leaf-insert-parent
-- nbtree-internal-insert-parent

--
-- Test incomplete internal page split
--
SELECT injection_points_attach('nbtree-insert-parent', 'error');
SELECT injection_points_attach('nbtree-finish-split', 'notice');
--
insert into nbtree_incomplete_splits select i from generate_series(9_000, 10_000) i;
SELECT injection_points_detach('nbtree-insert-parent');

-- Insert some more rows, finishing the split
insert into nbtree_incomplete_splits select i from generate_series(9_000, 10_000) i;
SELECT injection_points_detach('nbtree-finish-split');
