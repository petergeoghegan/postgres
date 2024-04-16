set work_mem='100MB';
set effective_cache_size='24GB';
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
set vacuum_freeze_min_age = 0;
set cursor_tuple_fraction=1.000;
create extension if not exists pageinspect; -- just to have it
reset client_min_messages;

-- Set log_btree_verbosity to 1 without depending on having that patch
-- applied (HACK, just sets commit_siblings instead when we don't have that
-- patch available):
select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '1', false);

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

----------------------
-- Misc multi tests --
----------------------
set client_min_messages=error;
drop table if exists multi_test_skip;
reset client_min_messages;

create unlogged table multi_test_skip(
  a int,
  b int
);

create index multi_test_skip_idx on multi_test_skip(a, b);

insert into multi_test_skip
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
vacuum analyze multi_test_skip;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Harder case
select * from multi_test_skip where a in (123, 182, 183) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test_skip where a in (123, 182, 183) and b in (1,2);

-- Hard case
select * from multi_test_skip where a in (182, 183, 184) and b in (1,2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test_skip where a in (182, 183, 184) and b in (1,2);

select * from multi_test_skip where a in (3,4,5) and b > 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test_skip where a in (3,4,5) and b > 0;

set enable_indexscan to on;

-- Backwards scan:
select * from multi_test_skip where a in (3,4,5) and b > 0
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test_skip where a in (3,4,5) and b > 0
order by a desc, b desc;

set enable_indexscan to off;

-- Redundant test:
select *
from multi_test_skip
where
  a in (1, 99, 182, 183, 184)
  and a > 183;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test_skip
where
  a in (1, 99, 182, 183, 184)
  and a > 183;
-- Redundant test, flip order:
select *
from multi_test_skip
where
  a > 183
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_skip
where
  a > 183
  and a in (1, 99, 182, 183, 184);

select *
from multi_test_skip
where
  a in (180, 345)
  and a in (230, 300);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_skip
where
  a in (180, 345)
  and a in (230, 300);

insert into multi_test_skip
select
  NULL,
  j
from
  generate_series(1, 10) j
order by j;
vacuum analyze multi_test_skip;

select *
from multi_test_skip
where b = 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test_skip
where b = 1;

-- Reset
set enable_indexonlyscan to on;
set enable_indexscan to on;
drop index multi_test_skip_idx;

-- Test that roll over logic doesn't increment sk_datum plus remove NULL sk
-- marking when it should just do the latter.  Bug found in NULLS FIRST case
-- after returning from pgConf.dev.
--
-- (June 3)
create index multi_test_skip_idx_nulls_first on multi_test_skip(a nulls first, b);

-- Insert INT_MIN value that had better not be overlooked here:
insert into multi_test_skip
select -2147483648, 1;
vacuum analyze multi_test_skip;

select *
from multi_test_skip
where b = 1
order by a nulls first, b limit 5;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test_skip
where b = 1
order by a nulls first, b limit 5;

----------------
-- UUID tests --
----------------

set client_min_messages=error;
DROP TABLE if exists uuid_tests;
reset client_min_messages;

create unlogged table uuid_tests
(
  skippy uuid,
  predval int4,
  i       int4
);
create index uuid_tests_idx on uuid_tests(skippy, predval);

with www as (
  select i, gen_random_uuid() urand from
  generate_series(1, 5) i
)
insert into uuid_tests
select '00000000-0000-0000-0000-000000000001', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '10000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '20000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '30000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '40000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '50000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '60000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '70000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '80000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '90000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'A0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'B0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'C0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'D0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'E0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'F0000000-0000-0000-0000-000000000000', j, j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFE', j, j from
  generate_series(1, 5000) j;
vacuum analyze uuid_tests;

-- Basic skip scan test case for UUID, a pass-by-reference type that can use
-- increment/decrement stuff:
select count(*)
from uuid_tests
where predval in (333, 4000, 4500, 5000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*)
from uuid_tests
where predval in (333, 4000, 4500, 5000);

-- Equivalent-ish range scan formulation (expected to do same accesses, and
-- give same answer):
select count(*)
from uuid_tests
where skippy between '00000000-0000-0000-0000-000000000000' and 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*)
from uuid_tests
where skippy between '00000000-0000-0000-0000-000000000000' and 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);

-- Equivalent-ish range scan formulation using > and < operators (expected to do same accesses, and
-- give same answer) -- stresses preprocessing with pass-by-reference types:
select count(*)
from uuid_tests
where skippy > '00000000-0000-0000-0000-000000000000' and skippy < 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*)
from uuid_tests
where skippy > '00000000-0000-0000-0000-000000000000' and skippy < 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);

-- For good luck, more inequality stuff designed to stress preprocessing code
-- (note that these are boundary cases):
prepare uuid_good_luck as
select count(*)
from uuid_tests
where skippy > '0FFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF' and skippy < 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFE'
and
predval in (333, 4000, 4500, 5000);

execute uuid_good_luck;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- COSTS OFF added to get stable test output
execute uuid_good_luck;
deallocate uuid_good_luck;

-----------------------------
-- create_index NULL tests --
-----------------------------

set client_min_messages=error;
DROP TABLE if exists onek_skipscan;
DROP TABLE if exists onek_with_null;
reset client_min_messages;

CREATE unlogged TABLE onek_skipscan (
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

\getenv abs_srcdir PG_ABS_SRCDIR
\set filename :abs_srcdir '/data/onek.data'
COPY onek_skipscan FROM :'filename';
VACUUM ANALYZE onek_skipscan;

CREATE unlogged TABLE onek_with_null AS SELECT unique1, unique2 FROM onek_skipscan ;
INSERT INTO onek_with_null (unique1,unique2) VALUES (NULL, -1), (NULL, NULL);
CREATE UNIQUE INDEX onek_with_null_unique2_unique1 ON onek_with_null (unique2,unique1);
SET enable_indexscan = ON;
SET enable_bitmapscan = ON;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL AND unique1 > 500;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique1 > 500;
DROP INDEX onek_with_null_unique2_unique1;
CREATE UNIQUE INDEX onek_with_null_unique2desc_unique1 ON onek_with_null (unique2 desc,unique1);
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL AND unique1 > 500;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique1 > 500;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IN (-1, 0, 1);
DROP INDEX onek_with_null_unique2desc_unique1;
CREATE UNIQUE INDEX onek_with_null_unique2descnullslast_unique1 ON onek_with_null (unique2 desc nulls last,unique1);
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL AND unique1 > 500;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique1 > 500;
DROP INDEX onek_with_null_unique2descnullslast_unique1;
CREATE UNIQUE INDEX onek_with_null_unique2ascnullsfirst_unique1 ON onek_with_null (unique2  nulls first,unique1);
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique2 IS NOT NULL;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NOT NULL AND unique1 > 500;
SELECT count(*) FROM onek_with_null WHERE unique1 IS NULL AND unique1 > 500;
DROP INDEX onek_with_null_unique2ascnullsfirst_unique1;
-- Check initial-positioning logic too
CREATE UNIQUE INDEX onek_with_null_unique2 ON onek_with_null (unique2);
SET enable_indexscan = ON;
SET enable_bitmapscan = OFF;
SELECT unique1, unique2 FROM onek_with_null
  ORDER BY unique2 LIMIT 2;
SELECT unique1, unique2 FROM onek_with_null WHERE unique2 >= 0
  ORDER BY unique2 LIMIT 2;
SELECT unique1, unique2 FROM onek_with_null
  ORDER BY unique2 DESC LIMIT 2;
SELECT unique1, unique2 FROM onek_with_null WHERE unique2 >= -1
  ORDER BY unique2 DESC LIMIT 2;
SELECT unique1, unique2 FROM onek_with_null WHERE unique2 < 999
  ORDER BY unique2 DESC LIMIT 2;
RESET enable_indexscan;
RESET enable_bitmapscan;

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

-----------------------
-- tenk1 test cases  --
-----------------------
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

CREATE INDEX tenk1_skipscan_four_unique1 ON tenk1_skipscan (four, unique1);

prepare tenk1_four_skipscan as
SELECT hundred, unique1 FROM tenk1_skipscan
 WHERE unique1 = 444;

execute tenk1_four_skipscan;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 30 hits
execute tenk1_four_skipscan;
deallocate tenk1_four_skipscan;

prepare tenk1_four_skipscan_with_saop as
select * from tenk1_skipscan where unique1 in (4444, 4445) limit 3;

execute tenk1_four_skipscan_with_saop;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 31 hits
execute tenk1_four_skipscan_with_saop;
deallocate tenk1_four_skipscan_with_saop;

-- Challenge here is to not do significantly worse than master branch's
-- traditional full index scan, since skipping isn't going to work here:
drop index tenk1_skipscan_four_unique1;
CREATE INDEX tenk1_skipscan_hundred_unique1 ON tenk1_skipscan (hundred, unique1);
prepare tenk1_fallback_to_regular_fullscan as
SELECT hundred, unique1 FROM tenk1_skipscan
 WHERE unique1 = 444;

execute tenk1_fallback_to_regular_fullscan;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 30 hits
execute tenk1_fallback_to_regular_fullscan;
deallocate tenk1_fallback_to_regular_fullscan;

drop index tenk1_skipscan_hundred_unique1;
CREATE INDEX tenk1_skipscan_two_four_twenty ON tenk1_skipscan (two, four, twenty);

-- This test case caught sloppiness in adding new "input" skip scan keys for
-- index attributes that already had = strategy scan keys:
set enable_indexonlyscan=off;
select distinct four, twenty from tenk1_skipscan
where four in (1, 2) and twenty in (1, 2);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select distinct four, twenty from tenk1_skipscan
where four in (1, 2) and twenty in (1, 2);
set enable_indexonlyscan=on;

-- Redundant attributes test related to bug where equality input keys
-- spuriously get their own skip input key:
select distinct two, four, twenty, hundred
from tenk1_skipscan
where
  four in (0, 1)
  and four in (1, 2)
  and twenty = 1
order by two, four, twenty;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select distinct two, four, twenty, hundred
from tenk1_skipscan
where
  four in (0, 1)
  and four in (1, 2)
  and twenty = 1
order by two, four, twenty;

drop index tenk1_skipscan_two_four_twenty;
create index on tenk1_skipscan (two, four, twenty, hundred);

prepare tenk1_two_four_twenty_hundred_inequal as
select count(*), two, four, twenty, hundred
from tenk1_skipscan
where
  four in (1, 2, 3)
  and four = 1
  and twenty in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)
  and hundred < 50
group by two, four, twenty, hundred
order by two, four, twenty, hundred;
execute tenk1_two_four_twenty_hundred_inequal;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute tenk1_two_four_twenty_hundred_inequal;
deallocate tenk1_two_four_twenty_hundred_inequal;

-------------------------------------------------------------------------------
-- upper range tenk1 test case based on aggregates.out from regression tests --
-------------------------------------------------------------------------------
create index on tenk1_skipscan (unique1);

-- Closest match for regression test:
select unique1
from tenk1_skipscan
where unique1 > 2147483647
limit 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select unique1
from tenk1_skipscan
where unique1 > 2147483647
limit 3;

-- Variant:
select unique1
from tenk1_skipscan
where unique1 >= 2147483647
limit 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select unique1
from tenk1_skipscan
where unique1 >= 2147483647
limit 3;

-- Variant:
select unique1
from tenk1_skipscan
where unique1 > 2147483648
limit 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select unique1
from tenk1_skipscan
where unique1 > 2147483648
limit 3;

-- Variant:
select unique1
from tenk1_skipscan
where unique1 < (-2147483649)
limit 3;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select unique1
from tenk1_skipscan
where unique1 < (-2147483649)
limit 3;

-- SAOP project regression test that fails on 32-bit platforms including CI (or when you
-- unset "#define USE_FLOAT8_BYVAL" locally):
prepare floatbyref_preproc as
select
  unique1
from
  tenk1_skipscan
where
  unique1 < 3
  and unique1 <(-1)::bigint;

-- Note: This doesn't fail reliably when run on horse server (with "#define
-- USE_FLOAT8_BYVAL" commented out), though using ../coredump-run.sh seems to
-- help it to fail
execute floatbyref_preproc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute floatbyref_preproc;
deallocate floatbyref_preproc;

---------------------------
-- Wisconsin table tests --
---------------------------

set client_min_messages=error;
drop table if exists wisconsin;
reset client_min_messages;

create unlogged table wisconsin
(
unique1 int4,
unique2 int4,
two int4,
four int4,
ten int4,
twenty int4,
onepercent int4,
tenpercent int4,
twentypercent int4,
fiftypercent int4,
unique3 int4,
evenonepercent int4,
oddonepercent int4,
stringu1 text,
stringu2 text,
string4 text
);

\set filename :abs_srcdir '/data/wisconsin.csv'
COPY wisconsin FROM :'filename' with (format csv, encoding 'win1252', header false, null $$$$, quote $$'$$); -- Fix the syntax highlighting: '

insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483646,
  2147483646,
  2147483646,
  2147483646,
  2147483646;
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483647,
  2147483647,
  2147483647,
  2147483647,
  2147483647;
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647);
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,(-2147483648),
(-2147483648),
(-2147483648),
(-2147483648),
(-2147483648);

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Two:
create index two_idx on wisconsin (two, unique1);

select unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 = 5555;

drop index two_idx;

-- Four:
create index four_idx on wisconsin (four, unique1);

-- Point lookup:
select unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

drop index four_idx;

-- Ten:
create index ten_idx on wisconsin (ten, unique1);

-- Point lookup:
select ten, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select ten, unique1 from wisconsin where unique1 = 5555;

-- Range instead of skip attribute on "ten":
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 412 hits
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;

-- Range instead of skip attribute on "ten", backwards scan:
set enable_bitmapscan to off;
set enable_indexscan to on;
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555 order by ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 333 hits
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555 order by ten desc, unique1 desc;
set enable_bitmapscan to on;
set enable_indexscan to off;

-- SAOP:
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

-- Contradictory qual:
select * from wisconsin where unique1 between 101 and 100;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits, patch 0 hits, since patch detects >= and <= related contradictoriness
select * from wisconsin where unique1 between 101 and 100;

drop index ten_idx;

-- Four, ten:
create index four_ten_idx on wisconsin (four, ten, unique1);

-- Point lookup, skips two cols (four and ten):
select unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1152 hits
select unique1 from wisconsin where unique1 = 5555;

-- Point lookup, skips one col (four), range on other col after that (ten):
select unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1152 hits
select unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;

-- Should be able to handle BETWEEN ranges with same value for >= and <=:
select unique1 from wisconsin where four between 0 and 0 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 290 hits, patch 33 hits
select unique1 from wisconsin where four between 0 and 0 and unique1 = 5555;

-- Missing predicate is in "intermediate" column (ten) here:
select unique1 from wisconsin where four = 0 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 290 hits, patch 33 hits
select unique1 from wisconsin where four = 0 and unique1 = 5555;

-- Missing predicate is in "intermediate" column (ten) here, plus we use a
-- SAOP for unique1 this time around:
select unique1 from wisconsin where four = 0 and unique1 in (41, 5555, 299118, 300000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 290 hits, patch 33 hits
select unique1 from wisconsin where four = 0 and unique1 in (41, 5555, 299118, 300000);

-- SAOP:
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1152 hits
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

-- backwards scans:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Simple backwards scan (failed once, simplified from next test case):
select four, ten, twenty, unique1
from wisconsin
where unique1 = 1
order by four desc, ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1156 hits
select four, ten, twenty, unique1
from wisconsin
where unique1 = 1
order by four desc, ten desc, unique1 desc;

-- SAOP backwards scan:
select unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1156 hits
select unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;

-- One omitted attribute (four) followed by two SAOPs
select four, ten, unique1
from wisconsin
where
  ten in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
  and unique1 in (41, 5555, 299118, 300000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  ten in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
  and unique1 in (41, 5555, 299118, 300000);

drop index four_ten_idx;

-- SAOP, DESC index, forward scan (forward relative to DESC direction):
create index four_desc_ten_desc_idx on wisconsin (four desc, ten desc, unique1 desc);
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1156 hits
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;

-- SAOP, DESC index, backward scan (backward relative to DESC direction):
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four, ten, unique1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 1156 hits
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four, ten, unique1;

drop index four_desc_ten_desc_idx;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Twenty:
create index twenty_idx on wisconsin (twenty, unique1);

-- Point lookup:
select unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

drop index twenty_idx;

-- Hundred/onepercent:
create index onepercent_idx on wisconsin (onepercent, unique1);

-- Point lookup:
select unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

drop index onepercent_idx;

-- Four, ten, twenty, unique1 (causes errors about attribute order from
-- _by_preprocess_keys):
create index on wisconsin (four, ten, twenty, unique1);
select *
from wisconsin
where
  ten between 1 and 10
  and twenty in (1, 2, 3)
  and unique1 in (84396, 217539, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- master 822 hits
select *
from wisconsin
where
  ten between 1 and 10
  and twenty in (1, 2, 3)
  and unique1 in (84396, 217539, 60814);
