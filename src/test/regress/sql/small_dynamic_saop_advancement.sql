set client_min_messages=error;
drop table if exists tenk1_skipscan;
reset client_min_messages;
CREATE UNLOGGED TABLE tenk1_skipscan (
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
ALTER TABLE tenk1_skipscan SET (autovacuum_enabled=off);

\getenv abs_srcdir PG_ABS_SRCDIR
\set filename :abs_srcdir '/data/tenk.data'
COPY tenk1_skipscan FROM :'filename';
VACUUM ANALYZE tenk1_skipscan;

--
--
-- (June 6) Check behavior with duplicate index column contents
--
-- From create_index.sql, failed when you first added support for skip scan on
-- an ocplass lacking a skip support routine
--
set client_min_messages=error;
drop table if exists dupindexcols;
reset client_min_messages;
CREATE UNLOGGED TABLE dupindexcols AS
  SELECT unique1 as id, stringu2::text as f1 FROM tenk1_skipscan;
CREATE INDEX dupindexcols_i ON dupindexcols (f1, id, f1 text_pattern_ops);
ANALYZE dupindexcols;

SELECT count(*) FROM dupindexcols
  WHERE f1 BETWEEN 'WA' AND 'ZZZ' and id < 1000 and f1 ~<~ 'YX';

-- Challenge here is to not do significantly worse than master branch's
-- traditional full index scan, since skipping isn't going to work here:
CREATE INDEX tenk1_skipscan_hundred_unique1 ON tenk1_skipscan (hundred, unique1);
prepare tenk1_fallback_to_regular_fullscan as
SELECT hundred, unique1 FROM tenk1_skipscan
 WHERE unique1 = 444;

execute tenk1_fallback_to_regular_fullscan;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 30 hits
execute tenk1_fallback_to_regular_fullscan;
deallocate tenk1_fallback_to_regular_fullscan;

--------------------------------------------------------------------------------------------------
-- (November 15) Cross-type opclasses from datetime_ops opfamily "> to >=" transformation tests --
--------------------------------------------------------------------------------------------------

set enable_seqscan to off;
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
set work_mem = 64;
set enable_sort = off;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

set client_min_messages=error;
drop table if exists timestamp_date_crosstype_test;
reset client_min_messages;

create unlogged table timestamp_date_crosstype_test(
  t timestamp,
  i int4
);

create index timestamp_date_crosstype_test_idx on timestamp_date_crosstype_test (t, i);

insert into timestamp_date_crosstype_test
select ('1995-01-01'::date + i)::timestamp + interval '1 second', i
from generate_series(0, 100) i;
vacuum analyze timestamp_date_crosstype_test;

-- This had better not increment "t > 1995-01-01" into "t >= 1995-01-02",
-- given that the underlying column is actually a timestamp, not a date:
select *
from timestamp_date_crosstype_test
where
  t > '1995-01-01'::date and i = 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from timestamp_date_crosstype_test
where
  t > '1995-01-01'::date and i = 0;

---------------
-- like_test --
---------------

drop INDEX tenk1_skipscan_hundred_unique1;
create index stringu1_tenthous_idx on tenk1_skipscan (stringu1, tenthous);
-- Other end of index this time:
prepare like_test as
select stringu1, tenthous from tenk1_skipscan
where
  stringu1 > 'Y'::text and stringu1 <= 'Z{'::text and tenthous in (25, 8787, 571)
order by stringu1, tenthous;
execute like_test;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute like_test;
deallocate like_test;

-- Same, but this time as a backwards scan:
prepare like_test as
select stringu1, tenthous from tenk1_skipscan
where
  stringu1 > 'Y'::text and stringu1 <= 'Z{'::text and tenthous in (25, 8787, 571)
order by stringu1 desc, tenthous desc;
execute like_test;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute like_test;
deallocate like_test;

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
--
-- XXX UPDATE (December 3) Not anymore, no more moving to right page when our
-- high key lacks an exact match for non-truncated columns.
select * from multi_test where a in (182, 183, 306) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 306) and b in (1,2); -- 4 buffer hits

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
-- XXX UPDATE (February 28): This is 4 index buffer hits due to the
-- restriction on inequalities required in the opposite-to-scan direction
-- only.
--
-- XXX UPDATE (October 16 2024) Behavioral update to
-- required-in-opposite-direction-only scan keys has made this case get only 3
-- buffer hits (this will get 4 buffer hits on Postgres 17, though).
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

--------------------------------
-- MDAM paper small test case --
--------------------------------
set client_min_messages=error;
drop table if exists sales_mdam_paper_small;
reset client_min_messages;

create unlogged table sales_mdam_paper_small
(
  dept int4,
  sdate date,
  item_class serial,
  store int4,
  item int4,
  total_sales numeric
);
create index mdam_small_idx on sales_mdam_paper_small(dept, sdate, item_class, store);
vacuum analyze sales_mdam_paper_small;

-- Load data
insert into sales_mdam_paper_small (dept, sdate, item_class, store, total_sales)
select
  dept,
  '1995-01-01'::date + sdate,
  item_class,
  store,
  (random() * 500.0) as total_sales
from
  generate_series(1, 1) dept,
  generate_series(1, 4) sdate,
  generate_series(1, 8) item_class,
  generate_series(1, 3) store;

-- Mixes range arrays with conventional SAOPs, leading to confusion about
-- boundary conditions:
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- Non-skip-support, repeat "Mixes range arrays with conventional SAOPs,
-- leading to confusion about boundary conditions":
set skipscan_skipsupport_enabled=false;
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
reset skipscan_skipsupport_enabled;

-- Same again, but this time we use different operators/constants to get the
-- same effective date range as original BETWEEN version:
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-03' and sdate <= '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-03' and sdate <= '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- Ditto:
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-03' and sdate < '1995-01-06'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-03' and sdate < '1995-01-06'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- range on date has no lower bound (or lower bound is -inf):
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate < '1995-01-04'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate < '1995-01-04'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- range on date has no upper bound (or upper bound is +inf):
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-04'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  sdate > '1995-01-04'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- Now don't omit dept key, without changing rows returned:
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  dept between 0 and 100
  and sdate between '1995-01-04' and '1995-01-05'
  and item_class = 3
  and store = 2
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  dept between 0 and 100
  and sdate between '1995-01-04' and '1995-01-05'
  and item_class = 3
  and store = 2
order by dept, sdate, item_class, store;
-- Now don't omit dept key, without changing rows returned (matches original
-- query by including conventional SAOPs to make it harder to get right):
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  dept between 0 and 100
  and sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
  ctid, dept, sdate, item_class, store
from sales_mdam_paper_small
where
  dept between 0 and 100
  and sdate between '1995-01-04' and '1995-01-05'
  and item_class in (1, 3, 5)
  and store in (2, 3)
order by dept, sdate, item_class, store;

-- (November 18) Want to get coleman regression fix working in most general
-- form, variant of redescend_numeric_test test case.
-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

set client_min_messages=error;
drop table if exists dont_skip_quickly;
reset client_min_messages;
create unlogged table dont_skip_quickly (district int8, warehouse int8, orderid int8, orderline int8);
insert into dont_skip_quickly
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
create index dont_skip_quickly_idx on dont_skip_quickly (district, warehouse, orderid, orderline) with (fillfactor=30);
-- prewarm
select count(*) from dont_skip_quickly;
vacuum analyze dont_skip_quickly;
---------------------------------------------------------------------------------

select * from dont_skip_quickly
where district in (1, 2) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from dont_skip_quickly
where district in (1, 2) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;

-- Confusion around NEXT or PRIOR values with
-- avoid-coleman-testcase-regressions patch
--
-- Note: unlike with original redescend_test version, this version has no
-- nearby NULLs to cause us problems -- yet we're no better off for it.
-- So maybe think about copying this test into the big test suite as-is, with
-- its own table named 'nextprior_regressfix_test'.
set client_min_messages=error;
drop table if exists nextprior_regressfix_test;
reset client_min_messages;
create unlogged table nextprior_regressfix_test (district int4, warehouse int4, orderid int4, orderline int4);
create index nextprior_regressfix_test_idx on nextprior_regressfix_test (district, warehouse, orderid, orderline) with (fillfactor=30);
insert into nextprior_regressfix_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;

-- prewarm
select count(*) from nextprior_regressfix_test;
vacuum analyze nextprior_regressfix_test;
---------------------------------------------------------------------------------

-- Bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- (November 19 2024) Never had this same problem with same query once it uses a backwards scan:
-- (Repeat without skip support, caught a bug in avoid-coleman-testcase-regressions patch)
set skipscan_skipsupport_enabled=false;
select * from nextprior_regressfix_test
where district in (1, 2, 3) and warehouse > 4 and orderid > 149 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nextprior_regressfix_test
where district in (1, 2, 3) and warehouse > 4 and orderid > 149 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
reset skipscan_skipsupport_enabled;

create index name_no_skip_support on tenk1_skipscan (string4, tenthous);

set enable_bitmapscan to on;
set enable_indexonlyscan to on;
set enable_indexscan to on;

-- This query shouldn't have to access more than one leaf page (the leftmost),
-- since string4 < 'AAAAxx' condition matches tuples that are before any
-- actual extant tuples from the index:
prepare just_scan_leftmost_page as
select string4, tenthous
from tenk1_skipscan
where string4 < 'AAAAxx' and tenthous = 21;

execute just_scan_leftmost_page;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- Expect only 2 buffer hits
execute just_scan_leftmost_page;
deallocate just_scan_leftmost_page;

-- Forwards scan
prepare terminate_name_ontime_inequality as
SELECT string4, tenthous FROM tenk1_skipscan
 WHERE string4 < 'OOOOxx' and tenthous in (131, 997, 5997, 992);

-- When first working on range skip scans + no-skip-support, had this one
-- remaining failure to terminate the scan when it reached the leaf page where
-- we naturally expect it to end.  The usual familiar assertion failure (with
-- a lack of any wrong answers to queries) proved as much:

-- TRAP: failed Assert("!_bt_tuple_before_array_skeys(scan, dir, tuple, tupdesc, tupnatts, false, 0, NULL)"), File: "../source/src/backend/access/nbtree/nbtutils.c", Line: 3063, PID: 497118
-- [0x55627e45e524] _bt_advance_array_keys: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtutils.c:3062
-- [0x55627e45d20a] _bt_checkkeys: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtutils.c:5070
-- [0x55627e450d7f] _bt_readpage: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtsearch.c:2288
-- [0x55627e4500fb] _bt_first: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtsearch.c:1955
-- [0x55627e449e74] btgettuple: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtree.c:259
-- [0x55627e436625] index_getnext_tid: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/index/indexam.c:590
execute terminate_name_ontime_inequality;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute terminate_name_ontime_inequality;
deallocate terminate_name_ontime_inequality;
