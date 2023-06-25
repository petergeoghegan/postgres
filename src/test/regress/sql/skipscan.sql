drop table if exists skippy_tbl;
create table skippy_tbl (bar int4);
create index skippy_idx on skippy_tbl(bar);
insert into skippy_tbl select i from generate_series(1,500) i;
vacuum analyze skippy_tbl;

set log_btree_verbosity=1;
set track_io_timing to off;
set enable_seqscan to off;

-- prewarm
select count(*) from skippy_tbl;
select count(*) from skippy_tbl;
select count(*) from skippy_tbl;
vacuum analyze skippy_tbl;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
-- Simple example:
select ctid, bar from skippy_tbl where bar in (2,3,4);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,3,4);
-- continuescan-on-highkey case should work:
select ctid, bar from skippy_tbl where bar in (365,366);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (365,366);
-- pivotsearch (first item on leftmost leaf page's right sibling page) case
-- should also work:
select ctid, bar from skippy_tbl where bar in (367,368);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (367,368);
-- Gap of one shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,4);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,4);
-- Gap of two shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,5);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,5);

-- Adjoining non-pivot tuples split only by leaf page high key should require
-- only one descent of btree, so second page is read by read next page path:
select ctid, bar from skippy_tbl where bar in (366,367);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (366,367);

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;
--select bar from skippy_tbl where bar in (2,3,4);
--EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
--select bar from skippy_tbl where bar in (2,3,4);

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
select ctid, bar from skippy_tbl where bar in (2,3,4);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,3,4);
-- Same as "simple example", but with duplicates:
insert into skippy_tbl(bar) values (22), (23), (23), (24), (24), (24);
vacuum analyze skippy_tbl;
select ctid, bar from skippy_tbl where bar in (22,23,24) order by bar;
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (22,23,24) order by bar;

-- 3 non-pivot tuple matches:
select * from skippy_tbl where bar in (362,365,366);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar in (362,365,366);

-- Large group of duplicates spanning many pages
insert into skippy_tbl select 555 from generate_series(1,3000) i;
vacuum analyze skippy_tbl;

-- Looks like this now:
-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬──────────────────────────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │             highkey              │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼──────────────────────────────────┤
-- │ 1 │     1 │     1 │    372 │      3 │          0 │   373 │     0 │                 0 │      16 │       688 │ (bar)=(367)                      │
-- │ 2 │     2 │     1 │    134 │      2 │          0 │   135 │     0 │                 0 │      16 │     5,448 │ (bar)=(555)                      │
-- │ 3 │     4 │     1 │  1,278 │      6 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(7,202)')  │
-- │ 4 │     5 │     1 │  1,278 │      7 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(13,124)') │
-- │ 5 │     6 │     1 │    444 │      3 │          0 │    39 │     0 │                 0 │      78 │     4,920 │ ∅                                │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴──────────────────────────────────┘

-- Scan blknos 2,4,5,6
--
-- This avoids continuescan termination on block 2, which used to happen due
-- to using the wrong scan key (the first, from 500 constant).
-- It's fixed, so now we switch to next SAOP element rather than
-- terminate _bt_first-wise/_bt_search-wise scan at that point
--
-- (So matches master branch, buffer-access-count-wise)
select count(*) from skippy_tbl where bar in (500,555);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from skippy_tbl where bar in (500,555);

-- Same again, almost -- just don't scan blkno 2 this time
--
-- This results in one useful _bt_search call, and another useless one that
-- should be avoided by realizing that we already ran out of tuples to output
-- at the end of the fist _bt_search (which doesn't return any 556 rows
-- either, since there is nothing to return).
--
-- (So matches master branch, buffer-access-count-wise)
select count(*) from skippy_tbl where bar in (555,556);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from skippy_tbl where bar in (555,556);

-- We do want to go through the root (3) to descend to the leftmost page (1) and then step to its right
-- sibling page (2):
select ctid, bar from skippy_tbl where bar in (1, 500);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (1, 500);

-- No infinite loops, please
select * from skippy_tbl where bar = any ('{365,366,368}');
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = any ('{365,366,368}');

-- "More than one so->numArrayKeys" test case (uses 2 SAOPs)
drop table if exists multi_test;
create table multi_test(a int, b int);
create index multi_test_idx on multi_test (a,b);
insert into multi_test select j, case when i < 14 then 0 else 1 end from generate_series(1,14) i, generate_series(1,400) j order by j,i;
vacuum analyze multi_test;

-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬──────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │   highkey    │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼──────────────┤
-- │ 1 │     1 │     1 │    854 │      4 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(62)  │
-- │ 2 │     2 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(123) │
-- │ 3 │     4 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(184) │
-- │ 4 │     5 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(245) │
-- │ 5 │     6 │     1 │    854 │      4 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(306) │
-- │ 6 │     7 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(367) │
-- │ 7 │     8 │     1 │    476 │      3 │          0 │    80 │     0 │                 0 │      49 │     3,908 │ ∅            │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴──────────────┘

-- Note: We're trying to avoid unexpected "Filter: " entries in index scan plans
-- Note: Right now we lose against master because we get confused about
-- continuescan condition and accidentally scan way too many pages to the
-- right.

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Simpler case
-- Should only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (183) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2);

-- Harder case
-- Should still only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Simpler case
-- Should only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (183) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2);

-- Harder case
-- Should still only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;

-- Simpler case
-- Should only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (183) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2);

-- Harder case
-- Should still only need to scan root page (3) plus a single leaf page (4):
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, COSTS OFF, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);

drop table skippy_tbl;
drop table multi_test;
