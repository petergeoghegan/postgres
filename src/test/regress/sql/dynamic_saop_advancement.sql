set work_mem='100MB';
set effective_io_concurrency=100;
set effective_cache_size='24GB';
set maintenance_io_concurrency=100;
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
set log_btree_verbosity=1;
set vacuum_freeze_min_age = 0;
create extension if not exists pageinspect; -- just to have it
reset client_min_messages;

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

-------------------------------
-- Basic single column tests --
-------------------------------
set client_min_messages=error;
drop table if exists skippy_tbl;
reset client_min_messages;

create unlogged table skippy_tbl(
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

-- Test non-SAOP case in passing, to avoid regressions in how we handle
-- more standard "boundary cases":
select * from skippy_tbl where bar = 366;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = 366;

select * from skippy_tbl where bar = 367;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from skippy_tbl where bar = 367;

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

-- (August 21) Infinite loop binary-search-array-keys bug test case:
insert into skippy_tbl select 2^31-1;

with a as (
  select
    i
  from
    generate_series(1, 150000) i
)
select count(*) from skippy_tbl
where bar = any(array[(select array_agg(i) from a)]);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
with a as (
  select
    i
  from
    generate_series(1, 150000) i
)
select count(*) from skippy_tbl
where bar = any(array[(select array_agg(i) from a)]);

-- Backwards scan (more or less equivalent)
set enable_sort = off;
with a as (
  select
    i
  from
    generate_series(1, 150000) i
)
select * from skippy_tbl
where bar = any(array[(select array_agg(i) from a)]) order by bar desc limit 50 offset 3000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
with a as (
  select
    i
  from
    generate_series(1, 150000) i
)
select * from skippy_tbl
where bar = any(array[(select array_agg(i) from a)]) order by bar desc limit 50 offset 3000;

-----------------------------------------------------------------------
-- "More than one so->numArrayKeys" test case (uses 2 SAOPs/columns) --
-----------------------------------------------------------------------
set client_min_messages=error;
drop table if exists multi_test;
reset client_min_messages;

create unlogged table multi_test(
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

-- Now as a backwards scan
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12)
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12)
order by a desc, b desc;

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
-- As a backwards scan:
select * from multi_test where a in (3,4,5) and b > 0
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b > 0
order by a desc, b desc;

-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b >= 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b >= 0;
-- As a backwards scan:
select * from multi_test where a in (3,4,5) and b >= 0
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b >= 0
order by a desc, b desc;

-- This time we make "b" search-type scankey required:
select * from multi_test where a in (3,4,5) and b < 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 0;
-- As a backwards scan:
select * from multi_test where a in (3,4,5) and b < 0
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 0
order by a desc, b desc;

-- Variant (for good luck)
select * from multi_test where a in (3,4,5) and b < 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 1;
-- As a backwards scan:
select * from multi_test where a in (3,4,5) and b < 1
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (3,4,5) and b < 1
order by a desc, b desc;

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
CREATE UNLOGGED TABLE tenk1_dyn_saop (
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
ALTER TABLE tenk1_dyn_saop SET (autovacuum_enabled=off);

\set filename :abs_srcdir '/data/tenk.data'
COPY tenk1_dyn_saop FROM :'filename';
CREATE INDEX tenk1_dyn_saop_thous_tenthous ON tenk1_dyn_saop (thousand, tenthous);
VACUUM ANALYZE tenk1_dyn_saop;
-------------------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
prepare regress_tenk1_inequality as
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

execute regress_tenk1_inequality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute regress_tenk1_inequality;
deallocate regress_tenk1_inequality;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
prepare regress_tenk1_inequality as
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

execute regress_tenk1_inequality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute regress_tenk1_inequality;
deallocate regress_tenk1_inequality;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;
prepare regress_tenk1_inequality as
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

execute regress_tenk1_inequality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute regress_tenk1_inequality;
deallocate regress_tenk1_inequality;

-- Now my own backwards scan variant, index-only scan:
prepare regress_tenk1_inequality_backwards as
SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand desc, tenthous desc;
execute regress_tenk1_inequality_backwards;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute regress_tenk1_inequality_backwards;
deallocate regress_tenk1_inequality_backwards;

------------------------------------------------------
-- Confusion about wraparound for high order column --
------------------------------------------------------

-- (September 13) This test case demonstrates the need to wraparound the
-- most significant column (which is "thousand" here) so that the scan will
-- terminate on the leftmost leaf page, without needlessly accessing further
-- leaf pages to the right.
--
-- This is surprisingly subtle, and seems like it's the only test case that'll
-- catch this.  Note that what I describe is independent of the issue covered
-- by the next test case (nonarray_equality_strategy_orderproc_required_both_stages)
-- which was all about not doing the required comparisons in both functions.
-- This is about __not__ resetting cur_elem to zero for the "thousand" array once
-- the scan gets past the last "thousand = 1" tuple.
--
-- (October 28) We want to not only get the expected number of buffer hits; we
-- also want to terminate the scan within even incrementally advancing the
-- array keys.  More concretely, it should look like this (and does, at the
-- time of writing):
--
-- _bt_advance_array_keys, tuple: (thousand, tenthous)=(2, 2), 0x7ff88b087ea0   <-- first (2, *) tuple
--   numberOfKeys: 2
--  - sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]
--  - sk_attno: 2, cur_elem 9001/20500, val: 9001 [NULLS LAST, ASC]
--  + sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]              <--- No changes here
--  + sk_attno: 2, cur_elem 9001/20500, val: 9001 [NULLS LAST, ASC]    <--- Nor here
--  _bt_advance_array_keys: returns false
-- _bt_readpage final: (thousand, tenthous)=(2, 2), 0x7ff88b087ea0, from non-pivot offnum 22 TID (93,20) ended page and scan
-- _bt_readpage stats: currPos.firstItem: 0, currPos.lastItem: 19, nmatching: 20 ✅
-- _bt_first: returning offnum 2 TID (344,23)
-- _bt_readnextpage: ScanDirectionIsForward() case ran out of pages to the right
-- _bt_readnextpage: BTScanPosInvalidate() called for currPos
-- _bt_steppage: _bt_readnextpage() returns false so we do too
-- btendscan

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
prepare must_wraparound_high_order_column as
with a as (
  select i from generate_series(0, 10500) i
)
select thousand, tenthous
from
  tenk1_dyn_saop
where thousand in (0, 1) and
tenthous = any (array[(select array_agg(i) from a)]);

execute must_wraparound_high_order_column;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute must_wraparound_high_order_column;
deallocate must_wraparound_high_order_column;

-- Same again, but backwards scan for good luck
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

prepare must_wraparound_high_order_column_desc as
with a as (
  select i from generate_series(0, 10500) i
)
select thousand, tenthous
from
  tenk1_dyn_saop
where thousand in (0, 1) and
tenthous = any (array[(select array_agg(i) from a)])
order by thousand desc, tenthous desc;

execute must_wraparound_high_order_column_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute must_wraparound_high_order_column_desc;
deallocate must_wraparound_high_order_column_desc;

-- (September 12) This test case decisively proves that we need to use
-- non-array required BTEqualStrategyNumber scan keys, both in the
-- precheck-current-keys function, and the function that actually advances the
-- array keys using tuple values.
--
-- For a while the test would fail (we'd do useless extra leaf page visits)
-- because I lacked the required infrastructure in at least one of these two
-- functions.  This had surprisingly little (no?) coverage before then.  This
-- test case makes it really obvious.
-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;
prepare nonarray_equality_strategy_orderproc_required_both_stages as
with a as (
  select i from generate_series(-1, 10000) i
)
select
  thousand,
  tenthous
from
  tenk1_dyn_saop
where thousand = 1 and tenthous = any (array[(select array_agg(i) from a)]);

execute nonarray_equality_strategy_orderproc_required_both_stages;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute nonarray_equality_strategy_orderproc_required_both_stages;
deallocate nonarray_equality_strategy_orderproc_required_both_stages;

-- Same again, but this time use a plain index scan for good luck:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
prepare nonarray_equality_strategy_orderproc_required_both_stages as
with a as (
  select i from generate_series(-1, 10000) i
)
select
  thousand,
  tenthous
from
  tenk1_dyn_saop
where thousand = 1 and tenthous = any (array[(select array_agg(i) from a)]);

execute nonarray_equality_strategy_orderproc_required_both_stages;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute nonarray_equality_strategy_orderproc_required_both_stages;
deallocate nonarray_equality_strategy_orderproc_required_both_stages;

-- Same again, but this time use a backwards scan for good luck:
prepare nonarray_equality_strategy_orderproc_required_both_stages_desc as
with a as (
  select i from generate_series(-1, 10000) i
)
select
  thousand,
  tenthous
from
  tenk1_dyn_saop
where thousand = 1 and tenthous = any (array[(select array_agg(i) from a)])
order by thousand desc, tenthous desc;
execute nonarray_equality_strategy_orderproc_required_both_stages_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute nonarray_equality_strategy_orderproc_required_both_stages_desc;

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- (September 19) But what about inequalities?
-- This is almost the same query as the last one, except we use < as a
-- replacement for =.  We should get the same number of buffer hits.
prepare nonarray_inequality as
with a as (
  select i from generate_series(-1, 10000) i
)
select
  thousand,
  tenthous
from
  tenk1_dyn_saop
where thousand < 2 and tenthous = any (array[(select array_agg(i) from a)]);

execute nonarray_inequality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 2 buffer hits, just like original = query
execute nonarray_inequality;
deallocate nonarray_inequality;

-- Same again, but this time a backwards scan for good luck:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
prepare nonarray_inequality_desc as
with a as (
  select i from generate_series(-1, 10000) i
)
select
  thousand,
  tenthous
from
  tenk1_dyn_saop
where thousand < 2 and tenthous = any (array[(select array_agg(i) from a)])
order by thousand desc, tenthous desc;

execute nonarray_inequality_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 2 buffer hits, just like original = query
execute nonarray_inequality_desc;
deallocate nonarray_inequality_desc;

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

---------------------------------
-- non-SK_BT_REQFWD test cases --
---------------------------------

-- Index-only scan:
VACUUM (freeze,analyze) tenk1_dyn_saop;
set enable_indexonlyscan to on;

-- Four is omitted here:
prepare four_omitted as
select
  count(*),
  two,
  twenty
from
  tenk1_dyn_saop
where
  two = 0
  and twenty in (9, 10)
group by
  two,
  twenty;

execute four_omitted;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute four_omitted;
deallocate four_omitted;

-- tenk1_idx_extra_column_in_middle puzzle #1
--
-- Tests non-SK_BT_REQFWD array scan keys.  There is a "gap" in the columns
-- represented here.

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

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
-- "four is not null" isn't like "four is null" in that it renders lower order
-- columns non-SK_BT_REQFWD.
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
--
-- ">= any (array[1, 2])" is an SAOP that it executed by getting an extreme
-- element once, during preprocessing.  This mustn't be confused for the SAOPs
-- we care about.  It also renders lower order columns non-SK_BT_REQFWD, which
-- we must look out for for the usual reasons.
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
-- (August 29) This variant of puzzle #1 was interesting back in July.  I'm
-- keeping it now out of paranoia.
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
-- (August 29) This variant of puzzle #1 was interesting back in July.  I'm
-- keeping it now out of paranoia.
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
-- optimizer, which makes things significantly less efficient for master (1009
-- buffers hit), and significantly more efficient for patch (7 buffers hit):
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

VACUUM tenk1_dyn_saop;
set enable_indexonlyscan to on;

-------------------------------------------------------------
-- Pathological case with a huge number of array constants --
-------------------------------------------------------------

-- This stresses "binary search for array keys" logic, verifying that we never
-- do very much work under a buffer lock.  Needed in mid to late August.

prepare binsearch_stress_forward as
with a as (
  select i from generate_series(0, 5000) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two, four, twenty;

prepare binsearch_stress_backwards as
with a as (
  select i from generate_series(-1, 5000) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two desc, four desc, twenty desc;

-- Forward and backwards variants both tested:
\if :testing_patch
  execute binsearch_stress_forward;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_forward;
  execute binsearch_stress_backwards;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_backwards;
\endif

-- Same again, but with extra values at the end of the key space to stress the
-- implementation:
insert into tenk1_dyn_saop select unique1, unique2, 4999, four, ten, twenty, hundred, thousand, twothousand, fivethous, tenthous, odd, even, stringu1, stringu2, string4 from tenk1_dyn_saop limit 1 offset 0;
insert into tenk1_dyn_saop select unique1, unique2, 5000, four, ten, twenty, hundred, thousand, twothousand, fivethous, tenthous, odd, even, stringu1, stringu2, string4 from tenk1_dyn_saop limit 1 offset 1;
insert into tenk1_dyn_saop select unique1, unique2, 5001, four, ten, twenty, hundred, thousand, twothousand, fivethous, tenthous, odd, even, stringu1, stringu2, string4 from tenk1_dyn_saop limit 1 offset 2;
insert into tenk1_dyn_saop select unique1, unique2,   -5, four, ten, twenty, hundred, thousand, twothousand, fivethous, tenthous, odd, even, stringu1, stringu2, string4 from tenk1_dyn_saop limit 1 offset 3;

-- Forward and backwards variants both tested:
\if :testing_patch
  execute binsearch_stress_forward;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_forward;
  execute binsearch_stress_backwards;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_backwards;
\endif

deallocate binsearch_stress_forward;
deallocate binsearch_stress_backwards;

-- Minor variant (500 rather than 5000) can independently break:
prepare binsearch_stress_variant as
with a as (
  select i from generate_series(0, 500) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two, four, twenty;
\if :testing_patch
  execute binsearch_stress_variant;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_variant;
\endif

deallocate binsearch_stress_variant;

-- Minor variant (500 rather than 5000) with a backwards scan, just for good luck:
prepare binsearch_stress_variant_backwards as
with a as (
  select i from generate_series(0, 500) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two desc, four desc, twenty desc;
\if :testing_patch
  execute binsearch_stress_variant_backwards;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_variant_backwards;
\endif

deallocate binsearch_stress_variant_backwards;

-- Another variant (4999 rather than 5000) can independently break:
prepare binsearch_stress_other_variant as
with a as (
  select i from generate_series(0, 4999) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two, four, twenty;
\if :testing_patch
  execute binsearch_stress_other_variant;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_other_variant;
\endif

deallocate binsearch_stress_other_variant;

-- Yet another variant (4666 rather than 5000 -- no high matches returned) might be able to independently
-- break, so be careful and include coverage for that case too:
prepare binsearch_stress_yav as
with a as (
  select i from generate_series(0, 4666) i
)
select
  count(*), two, four, twenty
from
  tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two, four, twenty;
\if :testing_patch
  execute binsearch_stress_yav;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute binsearch_stress_yav;
\endif

deallocate binsearch_stress_yav;


-----------------------------
-- Empty index stress test --
-----------------------------

-- (September 12) This test shows the importance of avoiding becoming confused
-- when we fail to reach _bt_readpage due to not having any pages in the index
-- (but having some array keys)
set client_min_messages=error;
drop table if exists empty_tenk1_dyn_saop;
reset client_min_messages;
create unlogged table empty_tenk1_dyn_saop
(
  like tenk1_dyn_saop including indexes
);

prepare empty_table_stress_test as
with a as (
  select i from generate_series(0, 5000) i
)
select
  count(*), two, four, twenty
from
  empty_tenk1_dyn_saop
where
  two = any (array[(select array_agg(i) from a)]) and
  four = any (array[(select array_agg(i) from a)]) and
  twenty = any (array[(select array_agg(i) from a)])
group by
  two, four, twenty
order by
  two, four, twenty;
\if :testing_patch
  execute empty_table_stress_test;
  EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
  execute empty_table_stress_test;
\endif

-- Here it's important not to charge too much for a ludicrously high number of
-- descents of the index that exceeds what is possible with the patch:
--
-- July 21: Same example as the one shown to Tomas on-list today.
--
-- Index-only scan to make this realistic/compelling:
VACUUM (freeze,analyze) tenk1_dyn_saop;
set enable_indexonlyscan to on;

prepare avoid_planner_hits as
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

execute avoid_planner_hits;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 9 buffer hits
execute avoid_planner_hits;

-- (August 26)
-- Same query, but we must force the use of tenk1_dyn_saop_idx_lowcard, which
-- failed in an independently interesting way alongside the prior query during
-- work on binary search for next key:
drop index tenk1_dyn_saop_idx_many_columns;
drop index tenk1_idx_extra_column_in_middle;

-- Same query with other index:
execute avoid_planner_hits;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 9 buffer hits
execute avoid_planner_hits;
deallocate avoid_planner_hits;

-- (August 26)
-- Recreate temporarily dropped tenk1_idx_extra_column_in_middle index from
-- before:
create index tenk1_idx_extra_column_in_middle on tenk1_dyn_saop(two,four,twenty);

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Same trick should work with leading attribute a non-SAOP:
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
-- Even with a leading inequality ("two !=0"), the patch does almost 10x fewer
-- buffer accesses than the master branch will.
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

-- (September 14)
--
-- Here we see contradictory scan keys on the column "two" during the start of
-- the first would-be primitive index scan where two = 1, but not any earlier
-- primitive scans.
prepare qual_on_two_not_okay_later_on as
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

execute qual_on_two_not_okay_later_on;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_two_not_okay_later_on;
deallocate qual_on_two_not_okay_later_on;

-- (October 30)
-- Proof that we need to be careful around truncated high key
-- attributes.  They must have their cur_elem set to 0 directly.
--
-- Here the truncated high key "(two, four, twenty, hundred)=(1, 1, 5)" is at
-- issue.  We must not advance the "hundred" scan key's cur_elem past 45
-- prematurely, because we'll miss matches on:
-- "(two, four, twenty, hundred)=(1, 1, 5, 45)".
--
-- Here is what the buggy version looked like (note that this involves two
-- attributes needing to advance at the same time, on "twenty" and "hundred"):
--
-- _bt_advance_array_keys, pivot tuple: (two, four, twenty, hundred)=(1, 1, 5), 0x7f268631e508
--  numberOfKeys: 4
--  - sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]
--  - sk_attno: 3, cur_elem 0/2, val: 1 [NULLS LAST, ASC]
--  - sk_attno: 4, cur_elem 2/2, val: 88 [NULLS LAST, ASC]
--  + sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]
--  + sk_attno: 3, cur_elem 2/2, val: 5 [NULLS LAST, ASC]      <--- correct
--  + sk_attno: 4, cur_elem 2/2, val: 88 [NULLS LAST, ASC]     <--- buggy
-- _bt_advance_array_keys: returns true
--
-- And here is the correct version:
--
-- _bt_advance_array_keys, pivot tuple: (two, four, twenty, hundred)=(1, 1, 5), 0x7fa6612ca508
--  numberOfKeys: 4
--  - sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]
--  - sk_attno: 3, cur_elem 0/2, val: 1 [NULLS LAST, ASC]
--  - sk_attno: 4, cur_elem 2/2, val: 88 [NULLS LAST, ASC]
--  + sk_attno: 1, cur_elem 1/1, val: 1 [NULLS LAST, ASC]
--  + sk_attno: 3, cur_elem 2/2, val: 5 [NULLS LAST, ASC]      <--- correct, as before
--  + sk_attno: 4, cur_elem 0/2, val: 1 [NULLS LAST, ASC]      <--- correct this time around
-- _bt_advance_array_keys: returns true
--
prepare high_key_first_elem_confusion as
select count(*), two, four, twenty, hundred
from tenk1_dyn_saop
where two in (0, 1) and four = 1 and twenty in (1, 2, 5) and hundred in (1, 45, 88)
group by two, four, twenty, hundred
order by two, four, twenty, hundred;

execute high_key_first_elem_confusion;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute high_key_first_elem_confusion;
deallocate high_key_first_elem_confusion;

-- (September 14) Similar to above, but it's earlier value of two, not later
-- ones
prepare qual_on_two_not_okay_earlier_on as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two > 0
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

execute qual_on_two_not_okay_earlier_on;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_two_not_okay_earlier_on;
deallocate qual_on_two_not_okay_earlier_on;

-- (September 14) Similar to above, but it's an equality this time
prepare qual_on_two_not_okay_earlier_on_equality as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two = 1
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

execute qual_on_two_not_okay_earlier_on_equality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_two_not_okay_earlier_on_equality;
deallocate qual_on_two_not_okay_earlier_on_equality;

-- (September 14) Similar to above, but it's multiple SAOP equalities this time
prepare qual_on_two_multiple_saops as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and two in (1, 2)
  and four in (1, 2, 3)
group by two, four, twenty, hundred
order by two, four, twenty, hundred;

execute qual_on_two_multiple_saops;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_two_multiple_saops;
deallocate qual_on_two_multiple_saops;

-- (October 18) Similar to above, but tries to break lack of support for the
-- full set of _bt_preprocess_keys() push-ups in stripped down version of
-- function:
prepare qual_on_two_skew as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two in(-1,0)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

-- (October 27) UPDATE: we are supposed to get (and do get) 174 buffer hits for this -- not 178
-- buffer hits.
--
-- At one point we got the following superfluous 4 page/buffer accesses at the
-- end of the scan, due to not recognizing that contradictory quals make "two = 1":
--
-- _bt_readpage: 🍀  7 with 12 offsets/tuples (leftsib 6, rightsib 8) ➡️
--  _bt_readpage first: (two, four, twenty, hundred)=(1, 1, 5, 5), 0x7faf243e1d80, from non-pivot offnum 2 TID (0,28) started page
--  _bt_readpage final: (two, four, twenty, hundred)=(1, 1, 13, 33), 0x7faf243e0508, continuescan high key check did not end scan so must continue to right sibling in next _bt_readpage call, if any
--  _bt_readpage stats: currPos.firstItem: 0, currPos.lastItem: -1, nmatching: 0 ❌
-- _bt_readnextpage: ScanDirectionIsForward() case reads right sibling blk 7
-- _bt_readpage: 🍀  8 with 12 offsets/tuples (leftsib 7, rightsib 9) ➡️
--  _bt_readpage first: (two, four, twenty, hundred)=(1, 1, 13, 33), 0x7faf243dfd80, from non-pivot offnum 2 TID (2,5) started page
--  _bt_readpage final: (two, four, twenty, hundred)=(1, 3, 3, 43), 0x7faf243de508, continuescan high key check did not end scan so must continue to right sibling in next _bt_readpage call, if any
--  _bt_readpage stats: currPos.firstItem: 0, currPos.lastItem: -1, nmatching: 0 ❌
-- _bt_readnextpage: ScanDirectionIsForward() case reads right sibling blk 8
-- _bt_readpage: 🍀  9 with 12 offsets/tuples (leftsib 8, rightsib 10) ➡️
--  _bt_readpage first: (two, four, twenty, hundred)=(1, 3, 3, 43), 0x7faf243ddd80, from non-pivot offnum 2 TID (0,10) started page
--  _bt_readpage final: (two, four, twenty, hundred)=(1, 3, 11, 71), 0x7faf243dc508, continuescan high key check did not end scan so must continue to right sibling in next _bt_readpage call, if any
--  _bt_readpage stats: currPos.firstItem: 0, currPos.lastItem: -1, nmatching: 0 ❌
-- _bt_readnextpage: ScanDirectionIsForward() case reads right sibling blk 9
-- _bt_readpage: 🍀  10 with 15 offsets/tuples (leftsib 9, rightsib 0) ➡️
--  _bt_readpage first: (two, four, twenty, hundred)=(1, 3, 11, 71), 0x7faf243b9d80, from non-pivot offnum 1 TID (0,15) started page
--  _bt_readpage final: (two, four, twenty, hundred)=(4999, 0, 0, 0), 0x7faf243b8298, from non-pivot offnum 13 TID (344,25) ended page and scan
--  _bt_readpage stats: currPos.firstItem: 0, currPos.lastItem: -1, nmatching: 0 ❌
-- _bt_readnextpage: ScanDirectionIsForward() case reads right sibling blk 10
--
-- Ultimate fix for this (which made us not do this unnecessary page visits) was to
-- teach _bt_preprocess_array_keys() to merge together arrays related to the
-- same attribute (in this example it's 2 arrays on the attribute named "two"),
-- plus some tweaks to the main _bt_advance_array_keys logic.
execute qual_on_two_skew;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 174 buffer hits (not 178)
execute qual_on_two_skew;
deallocate qual_on_two_skew;

-- (October 26) Similar to above, but lower order (though still SK_BT_REQFWD) scankey "four" this
-- time around:
prepare qual_on_four_skew as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two = 0 and
  -- This is effectively "four in (1,2)" once _bt_preprocess_keys is applied
  -- correctly:
  four in (-1, 0, 1, 2) and
  four in (1, 2, 3, 4, 5)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

execute qual_on_four_skew;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_four_skew;
deallocate qual_on_four_skew;

-- (October 19) Similar to above, but involves non-required scankeys only.
-- This test led to an assertion failure in new _bt_advance_array_keys
-- function.
prepare qual_on_four_nonrequired_skew as
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  four in (1, 2, 3)
  and four in (-1, 1, 2, 4)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

execute qual_on_four_nonrequired_skew;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_four_nonrequired_skew;
deallocate qual_on_four_nonrequired_skew;

-- (October 27) We expect to be able to detect = as contradictory, provided the
-- redundancy doesn't involve SK_SEARCHARRAY scan keys -- even when there is a
-- SK_SEARCHARRAY scan key nearby.
prepare qual_on_two_nonarray_contradictory as
select two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (-1, 0, 1) and four in (1, 2, 3)
  and two in(0, 1, 2)
  and two = (select -1+0.0 offset 0) and two = (select count(*) from pg_operator limit 1)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22);

execute qual_on_two_nonarray_contradictory;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute qual_on_two_nonarray_contradictory;
deallocate qual_on_two_nonarray_contradictory;

-----------------------------------------
-- functional_dependencies test cases  --
-----------------------------------------
set client_min_messages=error;
drop table if exists functional_dependencies;
reset client_min_messages;

CREATE UNLOGGED TABLE functional_dependencies (
    filler1 TEXT,
    filler2 NUMERIC,
    a INT,
    b TEXT,
    filler3 DATE,
    c INT,
    d TEXT
);
CREATE INDEX fdeps_abc_idx ON functional_dependencies (a, b, c);
-- prewarm
select count(*) from functional_dependencies;
vacuum analyze functional_dependencies;
INSERT INTO functional_dependencies (a, b, c, filler1)
     SELECT mod(i,100), mod(i,50), mod(i,25), i FROM generate_series(1,5000) s(i);
-----------------------------------------

-- Test case from regression tests that failed with binary-search-saop-array
-- work, even when all other tests in this file passed:
SELECT count(*) FROM functional_dependencies WHERE a IN (1, 51) AND b IN ('1', '2');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
SELECT count(*) FROM functional_dependencies WHERE a IN (1, 51) AND b IN ('1', '2');


select count(*) from functional_dependencies
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from functional_dependencies
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2);

-- Test case for missing key column in predicate -- simple
prepare functional_dependencies_norequired_one as
select count(*), a, b, c
from
  functional_dependencies
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c;

execute functional_dependencies_norequired_one;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one;
deallocate functional_dependencies_norequired_one;

-- Test case for missing key column in predicate -- must skip 14 and match on
-- 15 here, slightly trickier
prepare functional_dependencies_norequired_two as
select count(*), a, b, c
from
  functional_dependencies
where a = 65 and c in (14, 15)
group by a, b, c;

execute functional_dependencies_norequired_two;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two;
deallocate functional_dependencies_norequired_two;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three as
select count(*), a, b, c
from
  functional_dependencies
  where a in (44,94) and c in (18,19,20)
  group by a, b, c;

execute functional_dependencies_norequired_three;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three;
deallocate functional_dependencies_norequired_three;

prepare functional_dependencies_norequired_four as
select count(*), a, b, c
from
  functional_dependencies
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_four;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four;
deallocate functional_dependencies_norequired_four;

-- (September 12) This query is a notable example of cases where the current
-- conservative degree to which we speculatively visit the next sibling page
-- is just too conservative.  We should probably be keeping a running tally of
-- how our bets have worked out so far, etc.
prepare functional_dependencies_norequired_five as
select count(*), a, b, c
from
  functional_dependencies
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c;

execute functional_dependencies_norequired_five;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five;
deallocate functional_dependencies_norequired_five;

prepare functional_dependencies_norequired_six as
select count(*), a, b, c
from
  functional_dependencies
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_six;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six;
deallocate functional_dependencies_norequired_six;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Now do backwards scan equivalents of all functional_dependencies tests:
select a, b, c from functional_dependencies
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2)
order by a desc, b desc, c desc limit 52;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select a, b, c from functional_dependencies
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2)
order by a desc, b desc, c desc limit 52;

-- Test case for missing key column in predicate -- simple
prepare functional_dependencies_norequired_one_desc as
select count(*), a, b, c
from
  functional_dependencies
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_one_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one_desc;
deallocate functional_dependencies_norequired_one_desc;

-- Test case for missing key column in predicate -- must skip 14 and match on
-- 15 here, slightly trickier
prepare functional_dependencies_norequired_two_desc as
select count(*), a, b, c
from
  functional_dependencies
where a = 65 and c in (14, 15)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_two_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two_desc;
deallocate functional_dependencies_norequired_two_desc;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three_desc as
select count(*), a, b, c
from
  functional_dependencies
  where a in (44,94) and c in (18,19,20)
  group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_three_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three_desc;
deallocate functional_dependencies_norequired_three_desc;

prepare functional_dependencies_norequired_four_desc as
select count(*), a, b, c
from
  functional_dependencies
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_four_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four_desc;
deallocate functional_dependencies_norequired_four_desc;

-- (September 12) This query is a notable example of cases where the current
-- conservative degree to which we speculatively visit the next sibling page
-- is just too conservative.  We should probably be keeping a running tally of
-- how our bets have worked out so far, etc.
--
-- This is also true for the forward scan version of this query above
-- (comments from there are repeated here), so this isn't a case where we see
-- a perhaps-natural disadvantage for forward scans.
prepare functional_dependencies_norequired_five_desc as
select count(*), a, b, c
from
  functional_dependencies
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_five_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five_desc;
deallocate functional_dependencies_norequired_five_desc;

prepare functional_dependencies_norequired_six_desc as
select count(*), a, b, c
from
  functional_dependencies
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_six_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six_desc;
deallocate functional_dependencies_norequired_six_desc;

------------------------------------------------
-- functional_dependencies random test cases  --
------------------------------------------------

set client_min_messages=error;
drop table if exists functional_dependencies_random;
reset client_min_messages;

CREATE UNLOGGED TABLE functional_dependencies_random (
    filler1 TEXT,
    filler2 NUMERIC,
    a INT,
    b TEXT,
    filler3 DATE,
    c INT,
    d TEXT
);
CREATE INDEX fdeps_abc_random_idx ON functional_dependencies_random (a, b, c);
INSERT INTO functional_dependencies_random (a, b, c, filler1)
     SELECT mod(i, 5), mod(i, 7), mod(i, 11), i FROM generate_series(1,1000) s(i);
-- prewarm
select count(*) from functional_dependencies_random;
vacuum analyze functional_dependencies_random;

SELECT count(*) FROM functional_dependencies_random WHERE a IN (1, 51) AND b IN ('1', '2');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
SELECT count(*) FROM functional_dependencies_random  WHERE a IN (1, 51) AND b IN ('1', '2');

select count(*) from functional_dependencies_random
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*) from functional_dependencies_random
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2);

-- Test case for missing key column in predicate -- simple
prepare functional_dependencies_norequired_one as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c;

execute functional_dependencies_norequired_one;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one;
deallocate functional_dependencies_norequired_one;

-- Test case for missing key column in predicate -- must skip 14 and match on
-- 15 here, slightly trickier
prepare functional_dependencies_norequired_two as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 65 and c in (14, 15)
group by a, b, c;

execute functional_dependencies_norequired_two;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two;
deallocate functional_dependencies_norequired_two;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three as
select count(*), a, b, c
from
  functional_dependencies_random
  where a in (44,94) and c in (18,19,20)
  group by a, b, c;

execute functional_dependencies_norequired_three;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three;
deallocate functional_dependencies_norequired_three;

prepare functional_dependencies_norequired_four as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_four;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four;
deallocate functional_dependencies_norequired_four;

prepare functional_dependencies_norequired_five as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c;

execute functional_dependencies_norequired_five;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five;
deallocate functional_dependencies_norequired_five;

prepare functional_dependencies_norequired_six as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_six;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six;
deallocate functional_dependencies_norequired_six;

select a, b, c from functional_dependencies_random
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2)
order by a desc, b desc, c desc limit 52;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select a, b, c from functional_dependencies_random
where
  a in (1, 2, 26, 27, 51, 52, 76, 77)
  and b in ('1', '2', '26', '27')
  and c in (1, 2)
order by a desc, b desc, c desc limit 52;

-- Test case for missing key column in predicate -- simple
prepare functional_dependencies_norequired_one_desc as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_one_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one_desc;
deallocate functional_dependencies_norequired_one_desc;

prepare functional_dependencies_norequired_two_desc as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 65 and c in (14, 15)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_two_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two_desc;
deallocate functional_dependencies_norequired_two_desc;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three_desc as
select count(*), a, b, c
from
  functional_dependencies_random
  where a in (44,94) and c in (18,19,20)
  group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_three_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three_desc;
deallocate functional_dependencies_norequired_three_desc;

prepare functional_dependencies_norequired_four_desc as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_four_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four_desc;
deallocate functional_dependencies_norequired_four_desc;

prepare functional_dependencies_norequired_five_desc as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_five_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five_desc;
deallocate functional_dependencies_norequired_five_desc;

prepare functional_dependencies_norequired_six_desc as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_six_desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six_desc;
deallocate functional_dependencies_norequired_six_desc;

---------------------------------------------------------------------------------
-- Don't accidentally scan way too many leaf pages rather than re-descend tree --
---------------------------------------------------------------------------------
set client_min_messages=error;
drop table if exists redescend_test;
reset client_min_messages;
create unlogged table redescend_test (district int4, warehouse int4, orderid int4, orderline int4);
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

-----------------------------------
-- Backwards scan wraparound bug --
-----------------------------------

-- (October 21) This variant of redescend_test was discovered by random chance
-- when you stressed the implementation by varying BTREE_DEFAULT_FILLFACTOR.
--
-- Apparently wraparound during backwards scans results in buggy
-- behavior/confusion when the scan direction changes.
--
-- (October 28) This bug was related to wraparound from incremental
-- advancement of array keys.  Recall that every form of what you could call array
-- wraparound (including during incremental advancement of array keys) has
-- since been removed.

--set enable_bitmapscan to off;
--set enable_indexonlyscan to off;
--set enable_indexscan to off;
set enable_seqscan=off;

set client_min_messages=error;
drop table if exists backwards_wraparound_table;
reset client_min_messages;
create unlogged table backwards_wraparound_table (district int4, warehouse int4, orderid int4, orderline int4);
create index backwards_wraparound_index on backwards_wraparound_table (district, warehouse, orderid, orderline) with (fillfactor=30);
insert into backwards_wraparound_table
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- prewarm
select count(*) from backwards_wraparound_table;
vacuum analyze backwards_wraparound_table;
---------------------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Now test cursors that change the direction of the scan repeatedly, with
-- default scroll behavior:
set work_mem = 64;
set enable_sort = off;
-- forces index scan to get cursor to truly change directions in nbtree code
set cursor_tuple_fraction=1.000;
begin;
declare lose_place_cursor cursor for
select ctid, * from backwards_wraparound_table
where district in (1, 3) and warehouse in (3, 4, 5, 6, 7)
order by district, warehouse, orderid, orderline;
fetch forward 100 from lose_place_cursor;
fetch backward 50 from lose_place_cursor;
fetch forward 25 from lose_place_cursor;
/* lose_place_cursor */ commit;

------------------------
-- DESC columns tests --
------------------------
set client_min_messages=error;
drop table if exists skippy_tbl_desc;
reset client_min_messages;

create unlogged table skippy_tbl_desc(
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
create unlogged table nulls_test(
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
--
-- XXX Incorrect commentary:
--
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
--
-- XXX correction:
--
-- Update: (September 12) Well, that previous commentary was all wrong, and
-- likely written on autopilot just before leaving for NYC (the fact that this
-- was before I came up with the idea of using unlogged tables for better
-- insights into index page accesses in EXPLAIN ANALYZE output wouldn't have
-- helped, either).
--
-- In reality, this index should do 2 descents for two leaf pages, for a total
-- of 4 buffer accesses to the index itself, and 5 total (assuming one VM hit).
--
-- Update: (November 1) Recall that this test case went on to cause further
-- confusion in late October.  Today you figured out that this was really an
-- issue with _bt_binsrch_array_skey() not doing the right thing for backwards
-- scans, in that the progress of array key advancement wouldn't ratchet in
-- the scan direction (i.e. it was unlike forward scans once you actually
-- instrumented the binary searches themselves, even for the very simplest
-- cases with one page and two constants).
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc; -- 4 or 5 (depending on if you count the VM or not) buffer accesses

-- These don't hit "NOT_NULL > NULL" path, so they're just for good luck:
select * from nulls_test where a in (368,369) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (368,369) and b in (-1,-2,1) order by a desc nulls last, b desc;

-- Note: (September 4) This was the test case that was broken for some time,
-- unbeknownst to you.  You only figured this out when work on binary search
-- in arrays (the difficult work of getting the code to stop rescanning the
-- same tuple multiple times) was well underway.  Slightly unpleasant to learn
-- that I'd missed this earlier.
--
-- Here we have to visit leaf pages 7 and 8 (plus the root) because (since
-- this is a backwards scan) there's no way we can be sure that page 7 doesn't
-- have a tuple "(367, *)".  Of course the high key of page 7 would let us
-- know that, but we're coming from the right sibling to the left, so that's
-- not available (we could also choose to remember info from internal pages,
-- but seems not to be worth it to me)
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
create unlogged table coverage_null_compare_nonequal(a int4, b int4);
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
create unlogged table scankey_confusion(
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
--
-- (September 7) XXX:  This gets 3 buffer hits, instead of 2, as before
-- binary search on tuple stuff was in.  I think that that might be due to the
-- fact that one of the search-type scan keys isn't required by the scan.
-- We can maybe do better here, but let's not worry about it just yet.
--
-- (September 11) XXX: Yeah, I think that that's all it is.  It's probably an
-- example of a more general problem, though.  I think that any combination of
-- a inequality strategy scan key that's required.  So anything that looks
-- roughly like this + a high key comparison could be a problem
-- (SK_SEARCHNOTNULL is not relevant, SK_BT_REQFWD-only + high key is relevant):
--
-- _bt_preprocess_keys: output inkeys[1]: [ flags: [SK_ISNULL, SK_SEARCHNOTNULL, SK_BT_REQFWD]
--
-- Basically this happens because the second call to _bt_check_compare() is
-- (somewhat suspiciously) not allowed to set continuescan=false, no matter
-- what, in the case where _bt_advance_array_keys_locally() said that the
-- tuple has keys that equal all of the corresponding current array elements.
--
-- What's so special about BTEqualStrategyNumber, anyway? The only special
-- thing about them is the need to suppress continuescan=false when we're
-- too early (per _bt_first and _bt_checkkeys/_bt_check_compare comments).
--
-- (September 14) UPDATE: Yeah, this is now fixed once again -- now we go back
-- to not being confused about inequalities.  Recall that this happened when
-- you figured out (or more like stumbled upon) a way to not have to veto what the
-- second call to _bt_check_compare says about "continuescan" -- the call at
-- the end of _bt_checkkeys (after _bt_checkkeys has found matching equality/array
-- keys, leaving only the possibility of continuescan=false being set due to
-- required inequality type keys).
select * from scankey_confusion where a in (-1,0,1) and b is not null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (-1,0,1) and b is not null; -- Just 2 buffer hits (root + leftmost leaf)

-- Just for good luck, do full "is null" variants:
select * from scankey_confusion where a in (-1,0,1) and b is null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (-1,0,1) and b is null; -- 3 buffer hits (root and both leaf pages)

-- This one should only need rightmost page (along with root):
select * from scankey_confusion where a in (2,3,4,5) and b is null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (2,3,4,5) and b is null; -- Just 2 buffer hits (root + rightmost leaf)

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Should trip assertions:
select * from scankey_confusion where a in (-1,0,1) and b is not null;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from scankey_confusion where a in (-1,0,1) and b is not null; -- Just 2 buffer hits (root + leftmost leaf)

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
create unlogged table dont_be_too_conservative(
  a int,
  b int,
  c int
);
create index dont_be_too_conservative_idx on dont_be_too_conservative(a, b, c);
insert into dont_be_too_conservative select i, i, i from generate_series(1,500) i;
vacuum analyze dont_be_too_conservative;

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

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
create unlogged table nulls_first(
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

-- (September 8) Here we show a distilled case that demonstrates the need for
-- an opclass support function 1 (ORDER function) for every column that's
-- BTEqualStrategyNumber -- not just those that are SK_SEARCHARRAY array keys.
-- Recall that this was surprisingly unlikely to break queries.
--
-- If you remove the required comparator and just skip over relevant scan
-- keys when checking if a tuple needs to advance the array keys, you'll find
-- that this query apparently works as expected:
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{6}')
  and orderline = any ('{-5,500}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{6}')
  and orderline = any ('{-5,500}');
-- OTOH this very similar query fails:
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{7}')
  and orderline = any ('{-5,500}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{7}')
  and orderline = any ('{-5,500}');

-- The actual reason why the first query "succeeds" is that the page high key
-- for the relevant leaf page (block 5) looks like this:
--
-- (district, warehouse, orderid, anotherorderid, orderline)=(2, 1, null, 6)
--
-- So the only reason why the first variant would "succeed" was because
-- an "anotherorderid" of 6 made the high key seem to be within the bounds of
-- the array keys for the first query, but not the second query.  The second
-- query would repeat its access to page 5 because the state machine had the
-- wrong idea about our progress in the key space.
--
-- An additional complicating factor here is the interaction with suffix
-- truncation.  This variant of the failing query lacks "orderline = any
-- ('{-5,500}')", but is otherwise identical -- and so it always worked as
-- expected, even with the bug present:
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{7}');
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ctid, *
from nulls_first
where district = 1
  and warehouse = 5
  and orderid is null
  and anotherorderid = any ('{7}');

-- (September 8) XXX I think that it's okay to have no direct handling for other index
-- quals that are required in one direction only (i.e. for non-BTEqualStrategyNumber
-- required scan keys), though.  As far as I can tell the fallback on calling
-- _bt_advance_array_keys() when all else fails works for those cases.  But I
-- might be wrong about this.  I don't feel like being super thorough about it
-- right this second.

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

create unlogged table mark_restore_join_table1 (a int, b int);
create unlogged table mark_restore_join_table2 (a int, b int);
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

-- (September 22)
-- Test case that caused assertion failure related to not
-- having the right place in the scan for a merge join, in respect of
-- non-array equality-type scan keys.
set client_min_messages=error;
drop table if exists mark_restore_self_join;
reset client_min_messages;

create unlogged table mark_restore_self_join (a int, b int);
create index on mark_restore_self_join(a, b);

insert into mark_restore_self_join select 1, i from generate_series(1, 20) i;

vacuum analyze mark_restore_join_table1;
vacuum analyze mark_restore_join_table2;

select j1.ctid as j1_ctid, j2.ctid as j2_ctid, *
from
  mark_restore_self_join j1
    inner join
  mark_restore_self_join j2 on j1.a = j2.a
where j2.a = any (array[-1, 0, 1, 2, 3]) and j2.b = 5;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select j1.ctid as j1_ctid, j2.ctid as j2_ctid, *
from
  mark_restore_self_join j1
    inner join
  mark_restore_self_join j2 on j1.a = j2.a
where j2.a = any (array[-1, 0, 1, 2, 3]) and j2.b = 5;

-- (September 24) Repro for mark/restore bug affecting HEAD and all back branches
-- Per https://postgr.es/m/CAH2-WzkgP3DDRJxw6DgjCxo-cu-DKrvjEv_ArkP2ctBJatDCYg@mail.gmail.com
set client_min_messages=error;
drop table if exists amber_small;
drop table if exists amber_big;
reset client_min_messages;

create unlogged table amber_small
(
  a integer,
  b integer
);

create unlogged table amber_big
(
  a integer,
  b integer
);

insert into amber_big select 1,  2 from generate_series(1,1024);
insert into amber_big select 1,  3 from generate_series(1,1024);
insert into amber_big select 1,  5 from generate_series(1,1024);
insert into amber_big select 1,  6 from generate_series(1,1024);
insert into amber_big select 1,  7 from generate_series(1,1024);
insert into amber_big select 1,  8 from generate_series(1,1024);
insert into amber_big select 1, 10 from generate_series(1,1024);
insert into amber_big select 1, 12 from generate_series(1,1024);
insert into amber_big select 1, 13 from generate_series(1,1024);
insert into amber_big select 1, 15 from generate_series(1,1024);
insert into amber_big select 1, 17 from generate_series(1,1024);
insert into amber_big select 1, 19 from generate_series(1,1024);

insert into amber_small select 1,  1 from generate_series(1,8);
insert into amber_small select 1,  2 from generate_series(1,8);
insert into amber_small select 1,  3 from generate_series(1,8);
insert into amber_small select 1,  4 from generate_series(1,8);
insert into amber_small select 1,  5 from generate_series(1,8);
insert into amber_small select 1,  9 from generate_series(1,8);
insert into amber_small select 1, 10 from generate_series(1,8);
insert into amber_small select 1, 11 from generate_series(1,8);
insert into amber_small select 1, 12 from generate_series(1,8);
insert into amber_small select 1, 14 from generate_series(1,8);
insert into amber_small select 1, 17 from generate_series(1,8);
insert into amber_small select 1, 18 from generate_series(1,8);
insert into amber_small select 1, 19 from generate_series(1,8);

create index amber_big_idx on amber_big (a, b);
create index amber_small_idx on amber_small (a, b);

vacuum analyze amber_small;
vacuum analyze amber_big;

select count(*), small.a small_a
from
  amber_small small
    inner join
  amber_big big
    on small.a = big.a and small.b = big.b
where small.a in (1, 3) and big.a in (1, 3)
group by small_a order by small_a;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*), small.a small_a
from
  amber_small small
    inner join
  amber_big big
    on small.a = big.a and small.b = big.b
where small.a in (1, 3) and big.a in (1, 3)
group by small_a order by small_a;

reset enable_nestloop;
reset enable_hashjoin;
reset enable_sort;
reset enable_material;

--------------------------------
-- BooleanTest/BoolExpr tests --
--------------------------------
set client_min_messages=error;
drop table if exists boolindex;
reset client_min_messages;
create unlogged table boolindex (b bool, i int, unique(b, i), junk float);
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

reset enable_sort;

select 1
from pg_catalog.pg_collation c
where c.collencoding in (-1, 2) and c.collname ~ E'^(no\\.such\\.collation\\$)$';
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select 1
from pg_catalog.pg_collation c
where c.collencoding in (-1, 2) and c.collname ~ E'^(no\\.such\\.collation\\$)$';

--drop table skippy_tbl;
--drop table multi_test;
--drop table tenk1_dyn_saop;
--drop table functional_dependencies;
--drop table functional_dependencies_random;
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
