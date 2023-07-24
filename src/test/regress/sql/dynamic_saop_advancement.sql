set work_mem='100MB';
set effective_io_concurrency=100;
set effective_cache_size='24GB';
set maintenance_io_concurrency=100;
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
create extension if not exists pageinspect; -- just to have it
reset client_min_messages;

-------------------------------
-- Basic single column tests --
-------------------------------
set client_min_messages=error;
drop table if exists skippy_tbl;
reset client_min_messages;

create table skippy_tbl(
  bar int4
);

create index skippy_idx on skippy_tbl(bar);

insert into skippy_tbl
select
  i
from
  generate_series(1, 500) i;
-- prewarm
select count(*) from skippy_tbl;
vacuum analyze skippy_tbl;
-------------------------------

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
-- Simple example:
select ctid, bar from skippy_tbl where bar in (2,3,4);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,3,4);
-- Simple example of a backwards scan:
select ctid, bar from skippy_tbl where bar in (2,3,4) order by bar desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,3,4) order by bar desc;
-- continuescan-on-highkey case should work:
select ctid, bar from skippy_tbl where bar in (365,366);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (365,366);
-- pivotsearch (first item on leftmost leaf page's right sibling page) case
-- should also work:
select ctid, bar from skippy_tbl where bar in (367,368);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (367,368);
-- Gap of one shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,4);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,4);
-- Backwards scan gap of one shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,4) order by bar desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,4) order by bar desc;
-- Gap of two shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,5);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,5);
-- Backwards scan gap of two shouldn't confuse us:
select ctid, bar from skippy_tbl where bar in (2,5) order by bar desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,5) order by bar desc;

-- Adjoining non-pivot tuples split only by leaf page high key should require
-- only one descent of btree, so second page is read by read next page path:
select ctid, bar from skippy_tbl where bar in (366,367);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (366,367);
-- Equivalent backwards scan:
select ctid, bar from skippy_tbl where bar in (366,367) order by bar desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (366,367) order by bar desc;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;
-- This is the one that sometimes uses a sequential scan (when run as part of
-- the whole pg_regress suite):
select bar from skippy_tbl where bar in (2,3,4);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select bar from skippy_tbl where bar in (2,3,4);

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
select ctid, bar from skippy_tbl where bar in (2,3,4);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (2,3,4);
-- Same as "simple example", but with duplicates:
insert into skippy_tbl(bar) values (22), (23), (23), (24), (24), (24);
vacuum analyze skippy_tbl;
select ctid, bar from skippy_tbl where bar in (22,23,24) order by bar;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (22,23,24) order by bar;

-- 3 non-pivot tuple matches:
select * from skippy_tbl where bar in (362,365,366);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar in (362,365,366);

---------------------------------------------------
-- Large group of duplicates spanning many pages --
---------------------------------------------------
insert into skippy_tbl
select
  555
from
  generate_series(1, 3000) i;

vacuum analyze skippy_tbl;

-- Looks like this now:
--
-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬──────────────────────────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │             highkey              │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼──────────────────────────────────┤
-- │ 1 │     1 │     1 │    372 │      3 │          0 │   373 │     0 │                 0 │      16 │       688 │ (bar)=(367)                      │
-- │ 2 │     2 │     1 │    134 │      2 │          0 │   135 │     0 │                 0 │      16 │     5,448 │ (bar)=(555)                      │
-- │ 3 │     4 │     1 │  1,278 │      6 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(7,202)')  │
-- │ 4 │     5 │     1 │  1,278 │      7 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(13,124)') │
-- │ 5 │     6 │     1 │    444 │      3 │          0 │    39 │     0 │                 0 │      78 │     4,920 │ ∅                                │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴──────────────────────────────────┘
--
---------------------------------------------------

-- Scan blknos 2,4,5,6
--
-- This avoids continuescan termination on block 2, which used to happen due
-- to using the wrong scan key (the first, from 500 constant).
-- It's fixed, so now we switch to next SAOP element rather than
-- terminate _bt_first-wise/_bt_search-wise scan at that point
--
-- This does one less buffer access than master (only 5, not 6).  Master has
-- an extra root page access, which we can avoid.  It's only one less because
-- master does at least avoid visiting the same leaf page a second time in its
-- second _bt_first-wise scan of the index.
select count(*) from skippy_tbl where bar in (500,555);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from skippy_tbl where bar in (500,555);

-- Same again, almost -- just don't scan blkno 2 this time
--
-- This results in one useful _bt_search call.  We can avoid another useless
-- one by realizing that we already ran out of tuples to output at the end of
-- the fist _bt_search (which doesn't return any 556 rows either, since there
-- is nothing to return).
--
-- This variant of the query requires only 4 buffer accesses. As against 6
-- buffer accesses total in index for master branch.  Here we win by more
-- compared to last time (by 2 buffer accesses) because the master branch
-- wasn't so lucky about not having to visit the same leaf page a second time.
select count(*) from skippy_tbl where bar in (555,556);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from skippy_tbl where bar in (555,556);
-- The previous test case exercises how _bt_readpage deals with non-matches
-- covering key space > the highest non-pivot tuple in the index and < +inf.
-- Make sure that it continues to do that by checking the maximum value in the
-- index:
select max(bar) from skippy_tbl having max(bar) = 555; -- avoids regressing test coverage (i.e. tests the tests)

-- We do want to go through the root (3) to descend to the leftmost page (1) and then step to its right
-- sibling page (2):
-- XXX right now we don't do that -- what we actually do is redescend from the
-- root anew instead, just like the master branch -- so it's 4 buffer accesses
-- on the index instead of 3 accesses (we fall short of the obtainable
-- ideal, for now, since we're not yet able to be clever about using info from
-- internal pages -- nor are we willing to gamble even more aggressively).
select ctid, bar from skippy_tbl where bar in (1, 500);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, bar from skippy_tbl where bar in (1, 500);
-- Now show a similar case where even now we manage to get the obtainable
-- ideal (same path through the index is actually attained this time around).
-- This is possible here, independent of any speculative behavior and/or
-- cleverness when we descend the tree -- since the high key is 367, which
-- matches qual exactly. (We get only one descent and 3 buffer accesses,
-- versus master's 2 descents and 4 buffer accesses.  We manage to do better
-- than master, despite the fact that even master doesn't revisit the same
-- leaf page twice here -- master's only failing is that it touches the root
-- page a second time.)
select ctid, * from skippy_tbl where bar = any ('{365,367}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from skippy_tbl where bar = any ('{365,367}');

-- However, the patch isn't entirely free of such speculative behavior.
-- Here is a more complicated case that manages to be optimal -- though
-- barely.  Here we take a small gamble, and win.
--
-- This time around we don't have an exact leftmost page high key (367) match.
-- But we still win, since we do have 366 in both qual and in index (must be
-- both):
select * from skippy_tbl where bar = any ('{365,366,368}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = any ('{365,366,368}');
-- We were "between the values 366 and 368" at the point that we reached the
-- high key, whose value is 367 -- which is also "between" the same two
-- values.  On that basis alone we decided to move right.  We gambled and won.
-- This was a limited form of gamble that was only chosen because the only
-- value we were missing from page was the high key, 367.  The high key is a
-- little special here.
--
-- Now lets try almost the same case, just with 366 missing.  That has a
-- surprisingly big impact: now we won't gamble at all.  This time when we
-- compare our search-type scan key to the non-pivot 366, we didn't get a
-- match, AND we terminated the scan locally (we accepted continuescan=false).
select * from skippy_tbl where bar = any ('{365,368}');   -- omit non-pivot value '366' this time
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = any ('{365,368}');   -- 4 buffer accesses again

-- Now we'll show a gamble that doesn't pay off -- same rationale as earlier
-- gamble case, but this time we're not so lucky.  As a result, this query
-- needs an extra buffer access compared to master/no optimization case:
--
-- (This time we lose because 2147483647 isn't on the next page, despite it
-- seeming like it might.  XXX For now we'll accept this as a bad speculation;
-- a cost of doing business.  Might want to rereview that decision later on.)
--
-- Here we get 5 index buffer hits (one extra):
select * from skippy_tbl where bar = any ('{366,2147483647}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = any ('{366,2147483647}');  -- bad speculation

-----------------------------------------------------------------------
-- "More than one so->numArrayKeys" test case (uses 2 SAOPs/columns) --
-----------------------------------------------------------------------
set client_min_messages=error;
drop table if exists multi_test;
reset client_min_messages;

create table multi_test(
  a int,
  b int
);

create index multi_test_idx on multi_test(a, b);

insert into multi_test
select
  j,
  case when i < 14 then
    0
  else
    1
  end
from
  generate_series(1, 14) i,
  generate_series(1, 400) j
order by
  j,
  i;

vacuum analyze multi_test;

-- Looks like this now:
--
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
--
-----------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Simpler case
-- Should only need to scan root page (3) plus a single leaf page (4)
--
-- This means that _bt_checkkeys() continuescan handling mustn't get confused
-- about boundary conditions  in the presence of relatively complicated cases,
-- which this is -- multiple so->numArrayKeys is fairly rare.
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);

-- Harder case
-- Should only need to scan root page (3) plus a single leaf page (4).  This
-- is a bit trickier for _bt_checkkeys()-adjacent logic.
select * from multi_test where a in (123, 182, 183) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (123, 182, 183) and b in (1,2);

-- Hard case
-- Also needs to scan root page (3) plus leaf page 4 (like "Simpler case").
-- But this time we can't avoid going to a second leaf page -- leaf page 5.
-- That's where matches exceeding (184, -inf) are located.
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);

-- Hard luck case
-- Here we gamble and lose.  Almost like earlier skippy_tbl test case, but with
-- multiple SAOP columns for additional test coverage.  And, we only get a
-- single _bt_search because we were "almost correct".
--
-- That is, we descend from the root (3) to leaf page 4, which has matches.
-- Then we gamble by moving right on the leaf level, moving to sibling page 5,
-- which has no matches.  However, page 5 _does_ have a high key that makes us
-- want to move right again, to page 6 -- which is where our final match is
-- found!
select * from multi_test where a in (182, 183, 245) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 245) and b in (1,2); -- 4 buffer hits

-- Harder luck case
-- Two _bt_search descents this time (we _bt_first once we
-- reach page 5 because its high key indicates that it's time to quit gambling)
select * from multi_test where a in (182, 183, 306) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 306) and b in (1,2); -- 5 buffer hits

-- Not-SK_BT_REQFWD-but-still-insertion-scankey case
--
-- This is an example of how insertion scankey can have an attribute/value for
-- "b", even though "b" entry in search-type scankey doesn't end up SK_BT_REQFWD:
-- (_bt_array_continuescan actually encounters this directly, too)
select * from multi_test where a in (3,4,5) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b > 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b >= 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b >= 0;
-- This time we make "a" touch a boundary, in the style of "harder case":
select * from multi_test where a in (123, 182, 183) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (123, 182, 183) and b > 0; -- 2 buffer hits
-- This time we make "a" touch a boundary "inside the high key":
select * from multi_test where a in (123, 182, 183, 184) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (123, 182, 183, 184) and b > 0; -- 3 buffer hits

-- This time we make "b" search-type scankey required:
--
-- This is an example of the opposite: where an insertion scan key lacks an
-- entry corresponding to a search-type scankey's SK_BT_REQFWD entry.
-- (_bt_array_continuescan actually encounters this directly, too)
select * from multi_test where a in (3,4,5) and b < 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b < 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 1;
-- This time we make "a" touch a boundary, in the style of "harder case":
select * from multi_test where a in (123, 182, 183) and b < 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (123, 182, 183) and b < 3; -- 2 buffer hits
-- This time we make "a" touch a boundary "inside the high key":
select * from multi_test where a in (123, 182, 183, 184) and b < 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (123, 182, 183, 184) and b < 3; -- 3 buffer hits

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Simpler case
-- As above.
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);

-- Hard case
-- As above.
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);
-- Hard case backwards scan variant (just for coverage):
set enable_sort=off;
select * from multi_test where a in (182, 183, 184) and b in (1,2) order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2) order by a desc, b desc;
set enable_sort=on;

-- Hard luck case
-- As above.
select * from multi_test where a in (182, 183, 245) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 245) and b in (1,2);

-- Harder luck case
-- As above.
select * from multi_test where a in (182, 183, 306) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 306) and b in (1,2);

-- Not-SK_BT_REQFWD-but-still-insertion-scankey case
-- As above.
select * from multi_test where a in (3,4,5) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b > 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b >= 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b >= 0;
-- This time we make "b" search-type scankey required:
select * from multi_test where a in (3,4,5) and b < 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b < 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 1;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;

-- Simpler case
-- As above.
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);

-- Hard case
-- As above.
select * from multi_test where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2);

-- Hard luck case
-- As above.
select * from multi_test where a in (182, 183, 245) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 245) and b in (1,2);

-- Harder luck case
-- As above.
select * from multi_test where a in (182, 183, 306) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 306) and b in (1,2);

-- Not-SK_BT_REQFWD-but-still-insertion-scankey case
-- As above.
select * from multi_test where a in (3,4,5) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b > 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b >= 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b >= 0;
-- This time we make "b" search-type scankey required:
select * from multi_test where a in (3,4,5) and b < 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 0;
-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b < 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 1;

-------------------------------------------------------------------------------
-- tenk1 test cases involving queries where the optimization is inapplicable --
-------------------------------------------------------------------------------
set client_min_messages=error;
drop table if exists tenk1_dyn_saop;
reset client_min_messages;
\getenv abs_srcdir PG_ABS_SRCDIR
CREATE TABLE tenk1_dyn_saop (
	unique1		int4,
	unique2		int4,
	two			int4,
	four		int4,
	ten			int4,
	twenty		int4,
	hundred		int4,
	thousand	int4,
	twothousand	int4,
	fivethous	int4,
	tenthous	int4,
	odd			int4,
	even		int4,
	stringu1	name,
	stringu2	name,
	string4		name
);

\set filename :abs_srcdir '/data/tenk.data'
COPY tenk1_dyn_saop FROM :'filename';
CREATE INDEX tenk1_dyn_saop_thous_tenthous ON tenk1_dyn_saop (thousand, tenthous);
VACUUM ANALYZE tenk1_dyn_saop;
-------------------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

-- Microbenchmarks

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Low cardinality
CREATE INDEX tenk1_dyn_saop_idx_lowcard ON tenk1_dyn_saop (two, four, twenty, hundred);

-- Limit 10:
select ctid, * from tenk1_dyn_saop
where
  two in (0, 1)
  and four in (0, 1, 2)
  and twenty in (0, 1, 3)
  and hundred in (0, 1, 5)
order by
  two,
  four,
  twenty,
  hundred
limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from tenk1_dyn_saop
where
  two in (0, 1)
  and four in (0, 1, 2)
  and twenty in (0, 1, 3)
  and hundred in (0, 1, 5)
order by
  two,
  four,
  twenty,
  hundred
limit 10;

-------------------------------------
-- Optimizer indxpath.c test cases --
-------------------------------------

-- tenk1_idx_extra_column_in_middle puzzle #1
--
-- FIXME (July 12) We're losing big time against the master branch here.
--
-- Here we do not generate an index path with clauses for (two,four,twenty),
-- all because "four" doesn't appear in the query (only "two" and "twenty"
-- appear).  The most selective path is one with all 3 clauses as true index
-- quals (not as filter conditions/qpquals).
--
-- It is convenient (at least for now) for the optimizer to treat a "gap"
-- between the last attribute that had a SAOP clause and the next SAOP clause
-- as making it generally unsafe to include it as an additional SAOP clause.
-- This isn't exactly true; it just makes it unsafe to use the optimization at
-- runtime (clearly we could still have true index quals for all three of the
-- SAOPs/columns, since that's what we see on the master branch already).
--
-- We happen to lose big time here, but how often that happens in practice is
-- unclear.  See puzzle #2 for more information.
create index tenk1_idx_extra_column_in_middle on tenk1_dyn_saop(two,four,twenty);
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and twenty in (0, 1);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and twenty in (0, 1);

-- tenk1_idx_extra_column_in_middle puzzle #1.1
--
-- What's at issue here is whether or not "four is not null" should count as
-- an equality constraint
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and four is not null -- no value in "four" is ever a NULL
  and twenty in (0, 1);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and four is not null -- no value in "four" is ever a NULL
  and twenty in (0, 1);

-- tenk1_idx_extra_column_in_middle puzzle #1.2
--
-- What's at issue here is whether or not ">= any (array[1, 2])" should count as
-- an equality constraint
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and four >= any (array[1, 2]) -- ScalarArrayOpExr inequality
  and twenty in (0, 1);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and four >= any (array[1, 2]) -- ScalarArrayOpExr inequality
  and twenty in (0, 1);

-- tenk1_idx_extra_column_in_middle puzzle #2
--
-- (July 12) Here the patch gets the same plan shape as it did with puzzle #1.
--
-- This variation is interesting because now we see the patch do the same
-- thing as master (unlike with puzzle #1).  This at least suggests that the
-- regression from #1 isn't really a problem in practice: perhaps the cases
-- where we lose (in the style of puzzle #1) are insignificant -- #2 is very
-- close to #1 anyway, so even master barely managed to do the right thing for #1.
--
-- It would be convenient if I could continue to ignore the distinction
-- between safe-to-use-optimization and safe-to-have-as-index-quals, since I
-- don't want to have to have a bunch of semi-duplicative code in nbtutils.c
-- (better to keep all the safety stuff in one place, indxpath.c).
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and twenty in (0, 1, 2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  count(*)
from
  tenk1_dyn_saop
where
  two in (0, 1) -- i.e., every possible "two" value
  and twenty in (0, 1, 2);

-- tenk1_idx_extra_column_in_middle puzzle #3
--
-- Here the patch generates an SAOP index path with one one clause as an index
-- qual (for "two"), with everything else as a "Filter:"
create index tenk1_dyn_saop_idx_many_columns on tenk1_dyn_saop (two,four,twenty,unique1,hundred);
select ctid, *
from
  tenk1_dyn_saop
where
  two in (3, 5)
  and twenty in (0, 1, 3)
  and hundred in (0, 1, 5)
order by
  two,
  four,
  twenty
limit 15;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 2 buffer accesses (4 on master)
select ctid, *
from
  tenk1_dyn_saop
where
  two in (3, 5)
  and twenty in (0, 1, 3)
  and hundred in (0, 1, 5)
order by
  two,
  four,
  twenty
limit 15;

-- RhodiumToad test query from https://www.postgresql.org/message-id/flat/87egxzbn01.fsf%40news-spur.riddles.org.uk
--
-- RhodiumToad test #1
--
-- First let's see how it does with the existing tenk1_dyn_saop_thous_tenthous
-- index.  This case shows that your optimizer work makes sense.
--
-- Patch doesn't do all that much better than master here (126 buffer hits vs
-- 144), and yet if you give the master branch a choice between
-- tenk1_dyn_saop_thous_tenthous and the rhodium_toad index, it'll prefer to
-- use the latter one -- which actually works out to be about 3x more
-- expensive, buffer-hits-wise.
--
-- It's as if the master branch is (justifiably) afraid of using a SAOP
-- query with many constants, even when it would win without the nbtree work.
-- So as much as anything else the nbtree work serves to make it safe for the
-- optimizer to make that choice -- including those times where the nbtree
-- executor run time mechanisms don't actually improve much of anything.
select * from tenk1_dyn_saop
where
  thousand in (19, 29, 39, 49, 57, 66, 77, 8, 90, 12, 22, 32)
  and (ten >= 5) and (ten > 5 or unique1 > 5000)
order by ten, unique1 limit 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from tenk1_dyn_saop
where
  thousand in (19, 29, 39, 49, 57, 66, 77, 8, 90, 12, 22, 32)
  and (ten >= 5) and (ten > 5 or unique1 > 5000)
order by ten, unique1 limit 1;

-- RhodiumToad test #2
--
-- Same query, but now we have the rhodium_toad index available to the
-- optimizer, which makes things significantly less efficient:
--
-- Note: This is one of the few tests where we have more than a single restrictinfo
-- iclause for a single column
drop index tenk1_dyn_saop_thous_tenthous; -- have to force patch here
create index rhodium_toad on tenk1_dyn_saop(ten, unique1, thousand);
select * from tenk1_dyn_saop
where
  thousand in (19, 29, 39, 49, 57, 66, 77, 8, 90, 12, 22, 32)
  and (ten >= 5) and (ten > 5 or unique1 > 5000)
order by ten, unique1 limit 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from tenk1_dyn_saop
where
  thousand in (19, 29, 39, 49, 57, 66, 77, 8, 90, 12, 22, 32)
  and (ten >= 5) and (ten > 5 or unique1 > 5000)
order by ten, unique1 limit 1;

-- Nice demo of importance of work in context of ORDER BY ... LIMIT
-- Only 13 buffer hits on patch...but 1337 buffer hits on master!
select ctid, two, four, twenty from tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, two, four, twenty from tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;

-- Here it's important not to charge too much for a ludicrously high number of
-- descents of the index that exceeds what is possible with the patch:
--
-- July 21: Same example as the one shown to Tomas on-list today.
--
-- Index-only scan to make this realistic/compelling:
VACUUM tenk1_dyn_saop;
set enable_indexonlyscan to on;
select count(*), two, four, twenty from tenk1_dyn_saop
where
  two in (0, 1)
  and four in (1, 2, 3, 4)
  and twenty in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
group by
  two,
  four,
  twenty
order by
  two,
  four,
  twenty;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 9 buffer hits
select count(*), two, four, twenty from tenk1_dyn_saop
where
  two in (0, 1)
  and four in (1, 2, 3, 4)
  and twenty in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
group by
  two,
  four,
  twenty
order by
  two,
  four,
  twenty;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Same trick should work with leading attribute a non-SAOP:
drop index tenk1_dyn_saop_idx_many_columns; -- have to force
select ctid, thousand from tenk1_dyn_saop
where
  two = 0 and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, thousand from tenk1_dyn_saop
where
  two = 0 and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;

-- Same trick should work with middle attribute a non-SAOP:
select ctid, thousand from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (1, 2)
order by two, four, twenty limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, thousand from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (1, 2)
order by two, four, twenty limit 20;

-- Even an inequality on two should work, since we can get through non-matches
-- from the index quickly.
--
-- XXX (July 12) For now we accept parity with the master branch for this
-- case.  It's harder than I naively thought at first, so it might stay
-- unsupported for Postgres 17.
--
-- Why wouldn't doing this correctly require an approach like the one that the
-- MDAM paper outlines under "NOT = Predicates"?  Having "two != 0" as qpquals
-- isn't gonna cut it.
select ctid, thousand from tenk1_dyn_saop
where
  two != 0 and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, thousand from tenk1_dyn_saop
where
  two != 0 and four in (1, 2) and twenty in (1, 2)
order by two, four, twenty limit 20;

-- Same trick should work with interlaced SOAPs and non-SAOPS:
drop index tenk1_idx_extra_column_in_middle;
-- First variant:
select ctid, two, four, twenty, hundred
  from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (0, 1) and hundred = 1
order by two, four, twenty, hundred limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, two, four, twenty, hundred
  from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (0, 1) and hundred = 1
order by two, four, twenty, hundred limit 20;
-- Second variant:
select ctid, two, four, twenty, hundred
  from tenk1_dyn_saop
where
  two = 1 and four in (1, 2) and twenty = 1 and hundred in (0, 1)
order by two, four, twenty, hundred limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, two, four, twenty, hundred
  from tenk1_dyn_saop
where
  two = 1 and four in (1, 2) and twenty = 1 and hundred in (0, 1)
order by two, four, twenty, hundred limit 20;

-- Adversarial query that has a second clauses for the column "two".  One
-- clause (the SAOP) should be treated as an equality constraint, but, since
-- the other is an inequality it must invalidate the first.
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two < 1
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two < 1
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

---------------------------------------------------------------------------------
-- Don't accidentally scan way too many leaf pages rather than re-descend tree --
---------------------------------------------------------------------------------
set client_min_messages=error;
drop table if exists redescend_test;
reset client_min_messages;
create table redescend_test (district int4, warehouse int4, orderid int4, orderline int4);
create index must_not_full_scan on redescend_test (district, warehouse, orderid, orderline);
insert into redescend_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- prewarm
select count(*) from redescend_test;
vacuum analyze redescend_test;
---------------------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
-- Our goal here (and the only reasonable approach that's possible given all
-- the specifics) is to be at parity with the master branch, index-buffer-hit-wise.
select ctid, * from redescend_test where district in (1,2,3) and warehouse = 5 and orderid = 22;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from redescend_test where district in (1,2,3) and warehouse = 5 and orderid = 22;

--
-- Page 75 (from index 'must_not_full_scan') high key looks like this:
-- (district, warehouse, orderid, orderline)=(3, 3, 125)
--
-- This provides us with "NULL > NOT_NULL" coverage, which led to assertion
-- failure because this is a forward scan, contrary to my expectations:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124,125) and orderline is null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124,125) and orderline is null; -- 3 buffer hits
-- This should only need 2 buffer hits:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124) and orderline > 1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124) and orderline > 1000; -- 2 buffer hits
-- This should also only need 2 buffer hits:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124) and orderline < 1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124) and orderline < 1000; -- 2 buffer hits
-- This is gonna need 3, though:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,125) and orderline > 1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,125) and orderline > 1000; -- 3 buffer hits
-- This will also need 3:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,125) and orderline < 1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,125) and orderline < 1000; -- 3 buffer hits

-- Try it the other way -- 'NOT NULL' this time around:
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124,125) and orderline is not null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_test where district = 3 and warehouse = 3 and orderid in (121,124,125) and orderline is not null;

-- Can support both ScalarArrayOpExr-as-index-quals (with optimization enabled
-- at runtime) alongside RowCompareExpr -- though only when the RowCompareExpr
-- comes after the ScalarArrayOpExr.  Example:
select count(*) from redescend_test where district = 3 and warehouse in (4,5) and (orderid,orderline) >= (3,3);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from redescend_test where district = 3 and warehouse in (4,5) and (orderid,orderline) >= (3,3);
-- Ditto:
select count(*) from redescend_test where district = 3 and warehouse in (4,5) and (orderid,orderline) < (1,3);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from redescend_test where district = 3 and warehouse in (4,5) and (orderid,orderline) < (1,3);

-- Now test cursors that change the direction of the scan repeatedly, with
-- default scroll behavior:
set work_mem = 64;
set enable_sort = off;
-- forces index scan to get cursor to truly change directions in nbtree code
set cursor_tuple_fraction=1.000;
begin;
declare default_scroll_cursor cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;
fetch forward 100 from default_scroll_cursor;
fetch backward 50 from default_scroll_cursor;
fetch forward 25 from default_scroll_cursor;
fetch backward 15 from default_scroll_cursor;
fetch forward 5 from default_scroll_cursor;
-- Move to the end of the key space:
move forward 10000 in default_scroll_cursor;
-- See the last few rows there:
fetch backward 15 from default_scroll_cursor;
-- Move back to the start of the key space:
move backward 10000 in default_scroll_cursor;
-- See the first few rows there:
fetch forward 15 from default_scroll_cursor;
/* default_scroll_cursor */ commit;

-- Show EXPLAIN ANALYZE for cursor from transaction block (doing better than
-- this sems nontrivial):
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
declare default_scroll_cursor cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;

-- Same thing again, but with full scroll behavior this time around:
begin;
declare full_scroll_cursor scroll cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;
fetch forward 100 from full_scroll_cursor;
fetch backward 50 from full_scroll_cursor;
fetch forward 25 from full_scroll_cursor;
fetch backward 15 from full_scroll_cursor;
fetch forward 5 from full_scroll_cursor;
-- Move to the end of the key space:
move forward 10000 in full_scroll_cursor;
-- See the last few rows there:
fetch backward 15 from full_scroll_cursor;
-- Move back to the start of the key space:
move backward 10000 in full_scroll_cursor;
-- See the first few rows there:
fetch forward 15 from full_scroll_cursor;
/* full_scroll_cursor */ commit;

-- Show EXPLAIN ANALYZE for cursor from transaction block:
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
declare full_scroll_cursor scroll cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;

begin;
declare noscroll_cursor no scroll cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;
fetch forward 100 from noscroll_cursor;
fetch forward 25 from noscroll_cursor;
fetch forward 5 from noscroll_cursor;
-- Move to the end of the key space:
move forward 10000 in noscroll_cursor;
-- See the last few rows there:
fetch backward 15 from noscroll_cursor; -- fails this time around
/* noscroll_cursor */ abort;

-- Show EXPLAIN ANALYZE for cursor from transaction block:
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
declare noscroll_cursor no scroll cursor for
select * from redescend_test
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;

reset work_mem;
reset enable_sort;
reset cursor_tuple_fraction;

------------------------
-- DESC columns tests --
------------------------
set client_min_messages=error;
drop table if exists skippy_tbl_desc;
reset client_min_messages;

create table skippy_tbl_desc(
  bar int4
);
create index skippy_idx_desc on skippy_tbl_desc(bar desc);

insert into skippy_tbl_desc
select
  i
from
  generate_series(1, 500) i;
insert into skippy_tbl_desc
select
  555
from
  generate_series(1, 3000) i;

-- prewarm
select count(*) from skippy_tbl_desc;
vacuum analyze skippy_tbl_desc;

-- Looks like this now:
--
-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬──────────────────────────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │             highkey              │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼──────────────────────────────────┤
-- │ 1 │     1 │     1 │  1,278 │      6 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(7,196)')  │
-- │ 2 │     7 │     1 │  1,278 │      7 │          0 │     7 │     0 │                 0 │   1,115 │       312 │ (bar)=(555), (htid)=('(13,118)') │
-- │ 3 │     8 │     1 │    444 │      3 │          0 │    41 │     0 │                 0 │      75 │     4,888 │ (bar)=(500)                      │
-- │ 4 │     6 │     1 │     50 │      2 │          0 │    51 │     0 │                 0 │      16 │     7,128 │ (bar)=(450)                      │
-- │ 5 │     5 │     1 │    204 │      1 │          0 │   205 │     0 │                 0 │      16 │     4,048 │ (bar)=(246)                      │
-- │ 6 │     4 │     1 │    204 │      2 │          0 │   205 │     0 │                 0 │      16 │     4,048 │ (bar)=(42)                       │
-- │ 7 │     2 │     1 │     42 │      1 │          0 │    42 │     0 │                 0 │      16 │     7,308 │ ∅                                │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴──────────────────────────────────┘
--
-----------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
-- Don't get confused by DESC condition with pivot tuple high key termination:
select count(*) from skippy_tbl_desc where bar in (500,555);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from skippy_tbl_desc where bar in (500,555);
-- Backwards scan variant (doesn't use high key at all):
-- This is a convenient point to verify that backwards scans terminate without
-- redescending when there are a bunch of non-existent low sorting values on
-- the leftmost page (a similar test for forward scans happens elsewhere).
set enable_sort=off;
select distinct bar from skippy_tbl_desc where bar in (500,555,556,557,558,559,600) order by bar asc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select distinct bar from skippy_tbl_desc where bar in (500,555,556,557,558,559,600) order by bar asc;
set enable_sort=on;

----------------
-- NULLs test --
----------------

set client_min_messages=error;
drop table if exists nulls_test;
reset client_min_messages;
create table nulls_test(
  a int,
  b int
);

create index nulls_test_idx on nulls_test(a nulls first, b);

insert into nulls_test
select
  j,
  case when i < 14 then
    0
  else
    1
  end
from
  generate_series(1, 14) i,
  generate_series(1, 400) j
order by
  j,
  i;
insert into nulls_test
select
  NULL,
  case when i < 14 then
    0
  else
    1
  end
from
  generate_series(1, 14) i,
  generate_series(1, 400) j
order by
  j,
  i;

vacuum analyze nulls_test;

-- Looks like this now:
--
-- ┌────┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬───────────────────────────────────────┐
-- │ i  │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │                highkey                │
-- ├────┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼───────────────────────────────────────┤
-- │  1 │     1 │     1 │  1,271 │      7 │          0 │     7 │     0 │                 0 │   1,116 │       304 │ (a, b)=(null, 0), (htid)=('(30,188)') │
-- │  2 │    11 │     1 │  1,271 │      7 │          0 │     7 │     0 │                 0 │   1,116 │       304 │ (a, b)=(null, 0), (htid)=('(36,201)') │
-- │  3 │    12 │     1 │  1,271 │      7 │          0 │     7 │     0 │                 0 │   1,116 │       304 │ (a, b)=(null, 0), (htid)=('(42,214)') │
-- │  4 │    13 │     1 │  1,271 │      8 │          0 │     7 │     0 │                 0 │   1,116 │       304 │ (a, b)=(null, 0), (htid)=('(49,1)')   │
-- │  5 │    14 │     1 │    116 │      1 │          0 │   117 │     0 │                 0 │      24 │     4,872 │ (a, b)=(null, 1)                      │
-- │  6 │    10 │     1 │    778 │     28 │          0 │   113 │     0 │                 0 │      57 │     1,192 │ (a, b)=(28)                           │
-- │  7 │     9 │     1 │    476 │      3 │          0 │    69 │     0 │                 0 │      55 │     4,048 │ (a, b)=(62)                           │
-- │  8 │     2 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(123)                          │
-- │  9 │     4 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(184)                          │
-- │ 10 │     5 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(245)                          │
-- │ 11 │     6 │     1 │    854 │      4 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(306)                          │
-- │ 12 │     7 │     1 │    854 │      5 │          0 │   123 │     0 │                 0 │      55 │       808 │ (a, b)=(367)                          │
-- │ 13 │     8 │     1 │    476 │      3 │          0 │    80 │     0 │                 0 │      49 │     3,908 │ ∅                                     │
-- └────┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴───────────────────────────────────────┘
--
-----------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Just two buffer accesses here (one for root, the other for leaf page 14):
-- Note: provides coverage of "NULL < NOT_NULL" case
select count(*) from nulls_test where a is NULL and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from nulls_test where a is NULL and b in (1,2);

-- Need 7 buffer accesses here (one for root, another 6 for pages 1, 11, 12,
-- 13, 14, and 10):
-- Note: provides coverage of "NULL < NOT_NULL" case
select count(*) from nulls_test where a is NULL and b in (0,1);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from nulls_test where a is NULL and b in (0,1); -- shouldn't be visiting 9, though

-- On the other hand we'll only need 6 here (root, plus another 5 for pages 1,
-- 11, 13, and 14).
select count(*) from nulls_test where a is NULL and b in (-1, 0);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from nulls_test where a is NULL and b in (-1, 0); -- shouldn't be visiting 10, though

-- NULLS FIRST, but a backwards scan:
-- Note: provides coverage of "NOT_NULL > NULL" case
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;
select * from nulls_test where a in (1,2) and b in (-1,-2) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (1,2) and b in (-1,-2) order by a desc nulls last, b desc;

-- More complicated variant:
-- Note: (July 18) I initially overlooked a regression here, where backwards
-- scans would senselessly fail to skip several leaf pages (remember, it was
-- after I added emojis to debug log to make it easier to grasp the high level
-- structure with lots of output?).
--
-- I guess it's easier to overlook backwards scan issues, since you can't just
-- force a bitmap index scan to get index breakdown in EXPLAIN ANALYZE.
--
-- Note: (July 18) This is also an example of speculatively accessing the next
-- page during a backwards scan, while dealing with uncertainty about what's
-- on the next page.  Here we find some matches on the initial page we descend
-- onto, which encourages us to access its left sibling page -- which doesn't
-- work out.  If we didn't make this gamble then we'd only have 8 buffer
-- accesses instead of 9.  But it's an intelligent gamble that usually works
-- out (and works out in other test cases), so I say that this is worth it --
-- a bad speculation, but the cost of doing business.
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc; -- 9 (not 12) buffer accesses

-- These don't hit "NOT_NULL > NULL" path, so they're just for good luck:
select * from nulls_test where a in (368,369) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (368,369) and b in (-1,-2,1) order by a desc nulls last, b desc;

select * from nulls_test where a in (367,368) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (367,368) and b in (-1,-2,1) order by a desc nulls last, b desc;

select * from nulls_test where a in (366,367) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (366,367) and b in (-1,-2,1) order by a desc nulls last, b desc;

select * from nulls_test where a in (365,366) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (365,366) and b in (-1,-2,1) order by a desc nulls last, b desc;

----------------------------------------
-- Basic "NULL != NULL" code coverage --
----------------------------------------
set client_min_messages=error;
drop table if exists coverage_null_compare_nonequal;
reset client_min_messages;
create table coverage_null_compare_nonequal(a int4, b int4);
create index coverage_null_compare_nonequal_idx on coverage_null_compare_nonequal (a, b nulls first);
insert into coverage_null_compare_nonequal select i from generate_series(1,3) i;
insert into coverage_null_compare_nonequal select null from generate_series(1,2000);

-- Looks like this now:
--
-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬─────────────────────────────────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │                 highkey                 │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼─────────────────────────────────────────┤
-- │ 1 │     1 │     1 │      3 │      1 │          0 │     4 │     0 │                 0 │      22 │     8,044 │ (a, b)=(null)                           │
-- │ 2 │     2 │     1 │  1,319 │      5 │          0 │     7 │     0 │                 0 │   1,150 │        64 │ (a, b)=(null, null), (htid)=('(4,159)') │
-- │ 3 │     4 │     1 │    681 │      3 │          0 │   276 │     0 │                 0 │      24 │       180 │ ∅                                       │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴─────────────────────────────────────────┘
--
-----------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Hits the path that needs at least some coverage:
-- Note: provides coverage of "NOT_NULL < NULL" case
select * from coverage_null_compare_nonequal where a in (1,2,3);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from coverage_null_compare_nonequal where a in (1,2,3);

-------------------------------------------------
-- SK_BT_REQFWD-safety scankey confusion tests --
-------------------------------------------------

set client_min_messages=error;
drop table if exists scankey_confusion;
reset client_min_messages;
create table scankey_confusion(
  a int,
  b int
);

create index scankey_confusion_idx on scankey_confusion(a, b);
insert into scankey_confusion select 1, NULL from generate_series(1,1500);
insert into scankey_confusion select 0, NULL;

-- looks like this now:
--
-- ┌───┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬──────────────────────────────────────┐
-- │ i │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │               highkey                │
-- ├───┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼──────────────────────────────────────┤
-- │ 1 │     1 │     1 │  1,272 │      7 │          0 │     8 │     0 │                 0 │     980 │       276 │ (a, b)=(1, null), (htid)=('(5,141)') │
-- │ 2 │     2 │     1 │    229 │      2 │          0 │   229 │     0 │                 0 │      24 │     1,736 │ ∅                                    │
-- └───┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴──────────────────────────────────────┘
--
-----------------------------------------------------------------------

-- Make sure that (a, b)=(1, null), (htid)=('(5,141)') high key will be compared by
-- insertion scan key function if we have a regression and allow
-- SK_BT_REQFWD-less scankeys to be used again:
delete from scankey_confusion where a = 1 and b is null;
vacuum scankey_confusion;

-- Should trip assertions:
select * from scankey_confusion where a in (-1,0,1) and b is not null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (-1,0,1) and b is not null;

-- Just for good luck, do full "is null" variants:
select * from scankey_confusion where a in (-1,0,1) and b is null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (-1,0,1) and b is null; -- 3 buffer hits (root and both leaf pages)

-- This one should only need rightmost page (along with root):
select * from scankey_confusion where a in (2,3,4,5) and b is null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (2,3,4,5) and b is null; -- Just 2 buffer hits (root + rightmost leaf)

-------------------------------------------------------------------------------------------------------
-- Don't be too conservative about disabling optimization with low order column lacking SK_BT_REQFWD --
-------------------------------------------------------------------------------------------------------

set client_min_messages=error;
drop table if exists dont_be_too_conservative;
reset client_min_messages;
create table dont_be_too_conservative(
  a int,
  b int,
  c int
);
create index dont_be_too_conservative_idx on dont_be_too_conservative(a, b, c);
insert into dont_be_too_conservative select i, i, i from generate_series(1,500) i;
vacuum analyze dont_be_too_conservative;

-- Here the column c is in search type scan key, but isn't a SK_BT_REQFWD
-- column:
-- We expect to be able to use the optimization to good effect regardless.
-- This means that insertion scan key only contains an entry for a column,
-- while search type scankey contains entries for both a and c columns (though
-- only the first one will be SK_BT_REQFWD)
select * from dont_be_too_conservative where a in (2,3,4,5,6,7,8) and c = 7;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from dont_be_too_conservative where a in (2,3,4,5,6,7,8) and c = 7; -- just 2 buffer hits

----------------------------------------------
-- Don't get confused by NULLs FIRST column --
----------------------------------------------
set client_min_messages=error;
drop table if exists nulls_first;
reset client_min_messages;
create table nulls_first(
  district int4,
  warehouse int4,
  orderid int4,
  anotherorderid int4,
  orderline int4
);
create index nulls_first_idx on nulls_first(district, warehouse, orderid nulls first, anotherorderid, orderline);

insert into nulls_first
select district, warehouse, NULL, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 15) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;

-- prewarm
select count(*) from nulls_first;
vacuum analyze nulls_first;

-- looks like this now:
--
-- ┌────┬───────┬───────┬────────┬────────┬────────────┬───────┬───────┬───────────────────┬─────────┬───────────┬────────────────────────────────────────────────────────────────────────────┐
-- │ i  │ blkno │ flags │ nhtids │ nhblks │ ndeadhblks │ nlive │ ndead │ nhtidschecksimple │ avgsize │ freespace │                                  highkey                                   │
-- ├────┼───────┼───────┼────────┼────────┼────────────┼───────┼───────┼───────────────────┼─────────┼───────────┼────────────────────────────────────────────────────────────────────────────┤
-- │  1 │     1 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(1, 2, null, 6)  │
-- │  2 │     2 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(1, 3, null, 11) │
-- │  3 │     4 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      31 │       928 │ (district, warehouse, orderid, anotherorderid, orderline)=(1, 5)           │
-- │  4 │     5 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(2, 1, null, 6)  │
-- │  5 │     6 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(2, 2, null, 11) │
-- │  6 │     7 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      31 │       928 │ (district, warehouse, orderid, anotherorderid, orderline)=(2, 4)           │
-- │  7 │     8 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(2, 5, null, 6)  │
-- │  8 │     9 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(3, 1, null, 11) │
-- │  9 │    10 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      31 │       928 │ (district, warehouse, orderid, anotherorderid, orderline)=(3, 3)           │
-- │ 10 │    11 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(3, 4, null, 6)  │
-- │ 11 │    12 │     1 │    200 │      2 │          0 │   201 │     0 │                 0 │      32 │       912 │ (district, warehouse, orderid, anotherorderid, orderline)=(3, 5, null, 11) │
-- │ 12 │    13 │     1 │     50 │      2 │          0 │    50 │     0 │                 0 │      32 │     6,348 │ ∅                                                                          │
-- └────┴───────┴───────┴────────┴────────┴────────────┴───────┴───────┴───────────────────┴─────────┴───────────┴────────────────────────────────────────────────────────────────────────────┘
--
-----------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);

-- Now try IS NOT NULL variant:
select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is not null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is not null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);

select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);

-- Verifies that IS NOT NULL is not accepted as an equality constraint by optimizer:
select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is not null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is not null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);

-- RowCompare variant -- detects unsafe mixing of RowCompareExpr clauses with
-- ScalarArrayOpExr caluses
--
-- Make sure that we get this set of index quals, if at all possible:
-- Index Cond: ((ROW(district, warehouse) >= ROW(3, 3)) AND (orderid IS NULL) AND (anotherorderid = ANY ('{1,2}'::integer[]))
set cpu_operator_cost=0;
set random_page_cost=0;
-- We want to try to get such a plan to catch bugs where the optimizer allows
-- unsafe combinations of RowCompareExpr and ScalarArrayOpExr.

select ctid, * from nulls_first where (district, warehouse) >= (3,3) and orderid is null and anotherorderid = any ('{1,2}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first where (district, warehouse) >= (3,3) and orderid is null and anotherorderid = any ('{1,2}');

-- Okay, done.  Reset:
reset cpu_operator_cost;
set random_page_cost=2.0;

-- The same row constructor syntax works automatically (this doesn't even appear
-- as a RowCompare clause in the optimizer):
select ctid, * from nulls_first where (district, warehouse) = (3,3) and orderid is null and anotherorderid =  any ('{1,2}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, * from nulls_first where (district, warehouse) = (3,3) and orderid is null and anotherorderid =  any ('{1,2}');

--------------------------------------------------
-- Mark/restore ScalarArrayOpExr coverage tests --
--------------------------------------------------

set client_min_messages=error;
drop table if exists mark_restore_join_table1;
drop table if exists mark_restore_join_table2;
reset client_min_messages;

set enable_nestloop to 0;
set enable_hashjoin to 0;
set enable_sort to 0;
set enable_material to 0;

create table mark_restore_join_table1 (a int, b int);
create table mark_restore_join_table2 (a int, b int);
create index table1_idx on mark_restore_join_table1 (a) where a % 1000 = 1;
create index table2_idx on mark_restore_join_table2 (a) where a % 1000 = 1;

-- (July 13) Original regression tests had only 2 rows, I want more:
insert into mark_restore_join_table1 select 1, i from generate_series(1, 20) i;
insert into mark_restore_join_table2 select 1, i from generate_series(1, 20) i;

vacuum analyze mark_restore_join_table1;
vacuum analyze mark_restore_join_table2;

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Exercise array keys mark/restore B-Tree code
--
-- Note: This is one of the few tests where we have more than a single restrictinfo
-- iclause for a single column
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a = any (array[1]);

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a = any (array[1]);

-- Exercise array keys "find extreme element" B-Tree code
--
-- Note: This is one of the few tests where we have more than a single restrictinfo
-- iclause for a single column
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a >= any (array[1, 5]);

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a >= any (array[1, 5]);

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Exercise array keys mark/restore B-Tree code
-- As above
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a = any (array[1]);

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a = any (array[1]);

-- Exercise array keys "find extreme element" B-Tree code
-- As above
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a >= any (array[1, 5]);

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, * from
  mark_restore_join_table1 j1
  inner join mark_restore_join_table2 j2 on j1.a = j2.a and j1.b = j2.b
where
  j1.a % 1000 = 1 and j2.a % 1000 = 1 and j2.a >= any (array[1, 5]);

reset enable_nestloop;
reset enable_hashjoin;
reset enable_sort;
reset enable_material;

---------------------------------------------
-- BooleanTest/BoolExpr restrictinfo tests --
---------------------------------------------
set client_min_messages=error;
drop table if exists boolindex;
reset client_min_messages;
create table boolindex (b bool, i int, unique(b, i), junk float);
insert into boolindex select (i % 2 = 0), i from generate_series(1, 10) i;

-- "where b in ()" variants
select * from boolindex where b in (true, false) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b in (true, false) order by b, i limit 10;

select * from boolindex where b in (true, false) order by i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b in (true, false) order by i limit 10;

-- "where b" variants (Just more Var coverage)
select * from boolindex where b and i in (2,4,5) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b and i in (2,4,5) order by b, i limit 10;

select * from boolindex where b and i in (2,4,6) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b and i in (2,4,6) order by b, i limit 10;

-- "where b = true" variants (Just more Var coverage)
select * from boolindex where b = true and i in (2,4,5) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b = true and i in (2,4,5) order by b, i limit 10;

select * from boolindex where b = true and i in (2,4,6) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b = true and i in (2,4,6) order by b, i limit 10;

-- "where b is true" variants (BooleanTest coverage)
select * from boolindex where b is true and i in (2,4,5) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b is true and i in (2,4,5) order by b, i limit 10;

select * from boolindex where b is true and i in (2,4,6) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b is true and i in (2,4,6) order by b, i limit 10;

-- "where b = false" variants (BoolExpr coverage)
select * from boolindex where b = false and i in (2,4,5) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b = false and i in (2,4,5) order by b, i limit 10;

select * from boolindex where b = false and i in (2,4,6) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where b = false and i in (2,4,6) order by b, i limit 10;

-- "where not b" variants (more BoolExpr coverage)
select * from boolindex where not b and i in (2,4,5) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where not b and i in (2,4,5) order by b, i limit 10;

select * from boolindex where not b and i in (2,4,6) order by b, i limit 10;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from boolindex where not b and i in (2,4,6) order by b, i limit 10;

---------------------------------------------------
-- ORDER BY column comes first, SAOPs after that --
---------------------------------------------------
set client_min_messages=error;
drop table if exists docs_testcase;
reset client_min_messages;
select setseed(0.12345); -- Need deterministic test case
create table docs_testcase
(
  id serial,
  type text default 'pdf' not null,
  status text not null,
  sender_reference text not null,
  sent_at timestamptz,
  created_at timestamptz default '2000-01-01' not null
);
create index mini_idx on docs_testcase using btree(sent_at desc NULLS last, sender_reference, status);
insert into docs_testcase(type, status, sender_reference, sent_at)
select
  ('{pdf,doc,raw}'::text[]) [ceil(random() * 3)],
  ('{sent,draft,suspended}'::text[]) [ceil(random() * 3)],
  ('{Custom,Client}'::text[]) [ceil(random() * 2)] || '/' || floor(random() * 2000),
  ('2000-01-01'::timestamptz - interval '2 years' * random())::timestamptz
from
  generate_series(1, 100000) g;
vacuum analyze docs_testcase;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
set enable_sort to off;

select * from docs_testcase
where
  status in ('draft', 'sent') and
  sender_reference in ('Custom/1175', 'Client/362', 'Custom/280')
order by
  sent_at desc NULLS last
limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- need "costs off" here
select * from docs_testcase
where
  status in ('draft', 'sent') and
  sender_reference in ('Custom/1175', 'Client/362', 'Custom/280')
order by
  sent_at desc NULLS last
limit 20;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;

select sent_at, status, sender_reference from docs_testcase
where
  status in ('draft', 'sent') and
  sender_reference in ('Custom/1175', 'Client/362', 'Custom/280')
order by
  sent_at desc NULLS last
limit 20;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- need "costs off" here
select sent_at, status, sender_reference from docs_testcase
where
  status in ('draft', 'sent') and
  sender_reference in ('Custom/1175', 'Client/362', 'Custom/280')
order by
  sent_at desc NULLS last
limit 20;

reset enable_sort;

--drop table skippy_tbl;
--drop table multi_test;
--drop table tenk1_dyn_saop;
--drop table redescend_test;
--drop table skippy_tbl_desc;
--drop table nulls_test;
--drop table coverage_null_compare_nonequal;
--drop table scankey_confusion;
--drop table dont_be_too_conservative;
--drop table nulls_first;
--drop table mark_restore_join_table1;
--drop table mark_restore_join_table2;
--drop table boolindex;
