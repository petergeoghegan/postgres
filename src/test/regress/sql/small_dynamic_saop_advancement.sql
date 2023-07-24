set enable_seqscan = off;
set max_parallel_workers_per_gather=0;
\getenv abs_srcdir PG_ABS_SRCDIR

-- Set log_btree_verbosity to 1 without depending on having that patch
-- applied (HACK, just sets commit_siblings instead when we don't have that
-- patch available):
select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '1', false);

--
-- Heikki CREATE INDEX regression stress test query (miniaturized) --
--
-- Test case taken from: https://postgr.es/m/aa55adf3-6466-4324-92e6-5ef54e7c3918@iki.fi
--
-- set enable_seqscan=off; set max_parallel_workers_per_gather=0;

-- Setup:
set client_min_messages=error;
drop table if exists heikki_skiptest_small;
reset client_min_messages;

-- First do retail insert version of his query, where suffix truncation is
-- effective:
create unlogged table heikki_skiptest_small (a int, b int);
create index heikki_skiptest_small_idx on heikki_skiptest_small (a, b);

insert into heikki_skiptest_small
select g / 10 as a, g % 10 as b
from generate_series(1, 10_000) g;
vacuum freeze heikki_skiptest_small;

-- Forwards:
select count(*)
from heikki_skiptest_small
where b = 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*)
from heikki_skiptest_small
where b = 1;

-- Backwards:
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;

-- Okay, now the actual adversarial case, which requires that suffix
-- truncation wasn't very effective -- REINDEX to get that:
reindex index heikki_skiptest_small_idx;

-- Now repeat exactly the same queries as first time around:

-- Forwards:
select count(*)
from heikki_skiptest_small
where b = 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*)
from heikki_skiptest_small
where b = 1;

-- Backwards:
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;

-----------------------------------------------
-- Skip scan parity for backwards scan tests --
-----------------------------------------------
--
-- (Jan 29 2025) Make sure that backwards scan and forward scan full index
-- scans have reasonably (if not exactly) comparable performance
-- characteristics in cases where skip scan is applied but cannot ever really
-- help
--

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

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;

-- Forward scan:
select *
from multi_test
where b = 1
order by a, b;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test
where b = 1
order by a, b;

select *
from multi_test
where b < 0
order by a, b;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test
where b < 0
order by a, b;

-- Backward scan, should match forward scan "buffers" (more or less):
select *
from multi_test
where b = 1
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test
where b = 1
order by a desc, b desc;

select *
from multi_test
where b < 0
order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select *
from multi_test
where b < 0
order by a desc, b desc;

set client_min_messages=error;
drop table if exists high_low_high_card;
reset client_min_messages;
create unlogged table high_low_high_card(
  skippy int4,
  key int4,
  type text
);
create index on high_low_high_card(skippy, key);

insert into high_low_high_card
select
  -- "+ 250" here to make sure that there are leftmost pages full of tuples with
  -- distinct "skippy" vals:
  (abs(hashint4(i % 5)) + 250) % (10000 + 250),
  abs(hashint4(i + 42)) % 100_000,
  'fat'
from
  generate_series(1, 100_000) i;

with card as (
  select
    i skippy,
    abs(hashint4(i + j)) % 10000,
    'skinny'
  from
    generate_series(1, 10000) i,
    generate_series(1, 10) j
),
oth as (
  select
    *
  from
    card c
  where
    not exists (
      select
        *
      from
        high_low_high_card h
      where
        c.skippy = h.skippy)
)
insert into high_low_high_card
select
  *
from
  oth;
vacuum analyze high_low_high_card;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

--------------------
-- Forwards scans --
--------------------
select * from high_low_high_card where key = 40 order by skippy, key;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 40 order by skippy, key;

select * from high_low_high_card where key = 37 order by skippy, key;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 37 order by skippy, key;

select * from high_low_high_card where key = 38 order by skippy, key;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 38 order by skippy, key;

select * from high_low_high_card where key = 13 order by skippy, key;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 13 order by skippy, key;

select * from high_low_high_card where key = 15 order by skippy, key;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 15 order by skippy, key;

---------------------
-- Backwards scans --
---------------------
select * from high_low_high_card where key = 40 order by skippy desc, key desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 40 order by skippy desc, key desc;

select * from high_low_high_card where key = 37 order by skippy desc, key desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 37 order by skippy desc, key desc;

select * from high_low_high_card where key = 38 order by skippy desc, key desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 38 order by skippy desc, key desc;

select * from high_low_high_card where key = 13 order by skippy desc, key desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 13 order by skippy desc, key desc;

select * from high_low_high_card where key = 15 order by skippy desc, key desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select * from high_low_high_card where key = 15 order by skippy desc, key desc;

----------------
-- UUID tests --
----------------

set client_min_messages=error;
DROP TABLE if exists uuid_tests;
reset client_min_messages;

create unlogged table uuid_tests
(
  skippy uuid,
  predval int4
);
create index uuid_tests_idx on uuid_tests(skippy, predval);

insert into uuid_tests
select '00000000-0000-0000-0000-000000000001', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '10000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '20000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '30000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '40000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '50000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '60000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '70000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '80000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select '90000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'A0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'B0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'C0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'D0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'E0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'F0000000-0000-0000-0000-000000000000', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFE', j from
  generate_series(1, 5000) j;
insert into uuid_tests
select NULL, j from
  generate_series(1, 5000) j;
vacuum analyze uuid_tests;

-- Basic skip scan test case for UUID:
select skippy, predval
from uuid_tests
where predval = 777 order by skippy, predval;
-- The number of descents of the index significantly exceeds the number of
-- distinct "skippy" values, since we effectively probe for the next UUID
-- value by incrementing here.  Even though explicit probes aren't really used,
-- it more or less looks like they're used in practice.
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select skippy, predval
from uuid_tests
where predval = 777 order by skippy, predval;

-- Basic skip scan test case for UUID, backwards scan:
select skippy, predval
from uuid_tests
where predval = 777 order by skippy desc, predval desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select skippy, predval
from uuid_tests
where predval = 777 order by skippy desc, predval desc;

-- SAOP skip scan test case for UUID:
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
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- COSTS OFF added to get stable test output
select count(*)
from uuid_tests
where skippy between '00000000-0000-0000-0000-000000000000' and 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);

-- Equivalent-ish range scan formulation using > and < operators (expected to do same accesses, and
-- give same answer) -- stresses preprocessing with pass-by-reference types:
--
-- (UPDATE July 22) This arguably regressed a bit when we went from setting
-- low_elem and high_elem during preprocessing to always directly using the
-- inequalities to fix cross-type range bugs.  One extra primitive index scan.
select count(*)
from uuid_tests
where skippy > '00000000-0000-0000-0000-000000000000' and skippy < 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- COSTS OFF added to get stable test output
select count(*)
from uuid_tests
where skippy > '00000000-0000-0000-0000-000000000000' and skippy < 'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'
and
predval in (333, 4000, 4500, 5000);

-- For good luck, more inequality stuff designed to stress preprocessing code
-- (note that these are boundary cases):
--
-- (UPDATE July 22) This arguably regressed a bit when we went from setting
-- low_elem and high_elem during preprocessing to always directly using the
-- inequalities to fix cross-type range bugs.  One extra primitive index scan.
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

------------------------------------------------------------------------
-- (July 2) Alexander Alekseev failing "char" (not char(1)) test case --
------------------------------------------------------------------------
set client_min_messages=error;
drop table if exists alekseev_test;
reset client_min_messages;
create unlogged table alekseev_test(c "char", n bigint);

select setseed(0.5);
insert into alekseev_test
select chr(ascii('a') + random(0,2)) as c,
random(0, 1_000_000_000) as n
from generate_series(0, 10_000);

create index alekseev_test_idx on alekseev_test using btree(c, n);

-- Force bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Better not have a buggy btree/char_ops skip support function:
select count(*) from alekseev_test where n > 900_000_000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 11 buffer hits
select count(*) from alekseev_test where n > 900_000_000;

-- More selective query:
select c, n from alekseev_test where n = 952_200_397;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 952_200_397;

-- More selective query, low value that exists:
select c, n from alekseev_test where n = 24_759;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 24_759;

-- More selective query, low value that does not exist:
select c, n from alekseev_test where n = -1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = -1000;

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 8 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016);

-- Middling elements in SAOP test:
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 10 buffer hits
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);

-- Force index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

---------------------------------
-- "char" backwards scan tests --
---------------------------------

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 12 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;

-------------------------------------------------------
-- Text tests, which can't use skip support function --
-------------------------------------------------------

-- Make "more selective query" work with text, so we have somewhat of a basis
-- of comparison:
alter table alekseev_test alter column c type text;

-- Force bitmap index scan:
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

------------------------------------
-- Now repeat queries from before --
------------------------------------

-- "Better not have a buggy btree/char_ops skip support function", but no skip
-- support function this time around (since this is text):
select count(*) from alekseev_test where n > 900_000_000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 11 buffer hits
select count(*) from alekseev_test where n > 900_000_000;

-- More selective query:
select c, n from alekseev_test where n = 952_200_397;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 952_200_397;

-- More selective query, low value that exists:
select c, n from alekseev_test where n = 24_759;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 24_759;

-- More selective query, low value that does not exist:
select c, n from alekseev_test where n = -1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = -1000;

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016);

-- Middling elements in SAOP test:
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 14 buffer hits vs 10 for "char" due to not naturally needing to visit extremal n values
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);

-- Force index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-------------------------------
-- text backwards scan tests --
-------------------------------

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 12 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;

------------------------------
-- text NULL tests (July 7) --
------------------------------
insert into alekseev_test
select null, n from alekseev_test;

-- More selective query:
select c, n from alekseev_test where n = 952_200_397;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 952_200_397;

-- More selective query, low value that exists:
select c, n from alekseev_test where n = 24_759;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = 24_759;

-- More selective query, low value that does not exist:
select c, n from alekseev_test where n = -1000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where n = -1000;

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016);

-- Same again, but IS NOT NULL inequality used on skip attribute:
select c, n from alekseev_test where c is not null and n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where c is not null and n in (1, 24759, 999843016);

-- Same again, but with regular > inequality used on skip attribute:
select c, n from alekseev_test where c  > 'a' and n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where c  > 'a' and n in (1, 24759, 999843016);

-- Same again, but with regular < inequality used on skip attribute:
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016);

-- Middling elements in SAOP test:
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 14 buffer hits vs 10 for "char" due to not naturally needing to visit extremal n values
select c, n
from
  alekseev_test
where
n in (500_048_538,
      500_061_970,
      500_129_489,
      500_143_236,
      500_164_863,
      500_229_159,
      500_255_696,
      500_411_856,
      500_448_495,
      500_630_870);

---------------------------
-- NULL + Backwards scan --
---------------------------

-- Extremal elements so that scan visits leftmost and rightmost tuples within
-- each individual "c" grouping:
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 12 buffer hits
select c, n from alekseev_test where n in (1, 24759, 999843016) order by c desc, n desc;

-- Same again, but with IS NOT NULL on c:
select c, n from alekseev_test where c is not null and n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 12 buffer hits
select c, n from alekseev_test where c is not null and n in (1, 24759, 999843016) order by c desc, n desc;

-- Same again, but with regular > inequality used on skip attribute:
select c, n from alekseev_test where c  > 'a' and n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where c  > 'a' and n in (1, 24759, 999843016) order by c desc, n desc;

-- Same again, but with regular < inequality used on skip attribute:
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- parity with "char" test, 8 buffer hits
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016) order by c desc, n desc;

-- Almost the same query again, but now it's an <=:
select c, n from alekseev_test where c  <= 'b' and n in (1, 24759, 999843016) order by c desc, n desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where c  <= 'b' and n in (1, 24759, 999843016) order by c desc, n desc;

-- Same exact query again, but now it's a forwards scan:
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where c  < 'c' and n in (1, 24759, 999843016);

-- Almost the same query again, but now it's an <=:
select c, n from alekseev_test where c  <= 'b' and n in (1, 24759, 999843016);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select c, n from alekseev_test where c  <= 'b' and n in (1, 24759, 999843016);

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
vacuum analyze wisconsin;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Two:
create index two_idx on wisconsin (two, unique1);

select two, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select two, unique1 from wisconsin where unique1 = 5555;

drop index two_idx;

-- Four:
create index four_idx on wisconsin (four, unique1);

-- Point lookup:
select four, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select four, unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select four, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select four, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

drop index four_idx;

-- Ten:
create index ten_idx on wisconsin (ten, unique1);

-- Point lookup:
select ten, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select ten, unique1 from wisconsin where unique1 = 5555;

-- Range instead of skip attribute on "ten":
prepare range_instead as
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;

execute range_instead;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
execute range_instead;

-- Range instead of skip attribute on "ten", backwards scan:
set enable_bitmapscan to off;
set enable_indexscan to on;
prepare range_instead_backwards as
select ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555 order by ten desc, unique1 desc;

execute range_instead_backwards;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
execute range_instead_backwards;

set enable_bitmapscan to on;
set enable_indexscan to off;

-- SAOP:
select ten, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select ten, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

-- Contradictory qual:
-- XXX consider adding preprocessing to detect this case.
select ten, unique1 from wisconsin where unique1 between 101 and 100;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits, patch 0 hits, since patch detects >= and <= related contradictoriness
select ten, unique1 from wisconsin where unique1 between 101 and 100;

-- Contradictory qual, > and < strategies:
-- XXX consider adding preprocessing to detect this case.
select ten, unique1 from wisconsin where unique1 > 100 and unique1 < 100;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select ten, unique1 from wisconsin where unique1 > 100 and unique1 < 100;

-- Contradictory qual, > and < strategies, many operators:
-- XXX consider adding preprocessing to detect this case.
select ten, unique1 from wisconsin
where
unique1 > 1 and
unique1 < 400 and
unique1 > 90 and
unique1 > 100 and
unique1 < 100
;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select ten, unique1 from wisconsin
where
unique1 > 1 and
unique1 < 400 and
unique1 > 90 and
unique1 > 100 and
unique1 < 100
;

-- Just one constant generate by skip array (100):
select ten, unique1 from wisconsin where unique1 between 100 and 100;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select ten, unique1 from wisconsin where unique1 between 100 and 100;

-- Just one constant generate by skip array (100), > and < strategies:
select unique1 from wisconsin where unique1 > 99 and unique1 < 101;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select unique1 from wisconsin where unique1 > 99 and unique1 < 101;

-- Just one constant generate by skip array (100), > and < strategies, many operators:
select unique1 from wisconsin
where
unique1 > 1 and
unique1 < 400 and
unique1 > 90 and
unique1 > 99 and
unique1 < 101
;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select unique1 from wisconsin
where
unique1 > 1 and
unique1 < 400 and
unique1 > 90 and
unique1 > 99 and
unique1 < 101
;

drop index ten_idx;

-- Four, ten:
create index four_ten_idx on wisconsin (four, ten, unique1);

-- Point lookup, skips two cols (four and ten):
select four, ten, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 1152 hits
select four, ten, unique1 from wisconsin where unique1 = 5555;

-- Point lookup, skips one col (four), range on other col after that (ten):
select four, ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 1152 hits
select four, ten, unique1 from wisconsin where ten between -10000 and 4 and unique1 = 5555;

-- Should be able to handle BETWEEN ranges with same value for >= and <=:
prepare handle_between as
select four, ten, unique1 from wisconsin where four between 0 and 0 and unique1 = 5555;

execute handle_between;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
execute handle_between;

-- Missing predicate is in "intermediate" column (ten) here:
select four, ten, unique1 from wisconsin where four = 0 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 290 hits, patch 33 hits
select four, ten, unique1 from wisconsin where four = 0 and unique1 = 5555;

-- Missing predicate is in "intermediate" column (ten) here, plus we use a
-- SAOP for unique1 this time around:
select four, ten, unique1 from wisconsin where four = 0 and unique1 in (41, 5555, 299118, 300000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 290 hits, patch 33 hits
select four, ten, unique1 from wisconsin where four = 0 and unique1 in (41, 5555, 299118, 300000);

-- SAOP:
select four, ten, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 1152 hits
select four, ten, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

-- backwards scans:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Simple backwards scan (failed once, simplified from next test case):
select four, ten, unique1
from wisconsin
where unique1 = 1
order by four desc, ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 1156 hits
select four, ten, unique1
from wisconsin
where unique1 = 1
order by four desc, ten desc, unique1 desc;

-- SAOP backwards scan:
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 1156 hits
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;

-- One omitted attribute (four) followed by two SAOPs
select four, ten, unique1
from wisconsin
where
  ten in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
  and unique1 in (41, 5555, 299118, 300000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
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
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 1156 hits
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four desc, ten desc, unique1 desc;

-- SAOP, DESC index, backward scan (backward relative to DESC direction):
select four, ten, unique1
from wisconsin
where unique1 in (1, 5555, 100000, 200000)
order by four, ten, unique1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF) -- master 1156 hits
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
select twenty, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select twenty, unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select twenty, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select twenty, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

drop index twenty_idx;

-- Hundred/onepercent:
create index onepercent_idx on wisconsin (onepercent, unique1);

-- Point lookup:
select onepercent, unique1 from wisconsin where unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select onepercent, unique1 from wisconsin where unique1 = 5555;

-- SAOP:
select onepercent, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select onepercent, unique1 from wisconsin where unique1 in (1, 5555, 100000, 200000);

-- (June 14, day after going into JFK 27 to have lunch (later dinner) with
-- jkatz)
--
-- Test case showing bug in _bt_advance_skip_array_increment when we wrap
-- around a scan key whose sk_argument is already INT_MAX.  We should
-- "increment" its scan key to NULL, the true final value.  The bug that we
-- saw here involved not returning the row with a NULL oncepercent and a
-- unique1 value of -666.  We'd actually skip straight past NULL, wrapping
-- back around to INT_MIN again, which was just wrong.
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, 2147483647 from generate_series(1, 1500) i;
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, (-2147483648)::int4 from generate_series(1, 1500) i;

insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, 2147483647;
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, (-2147483648)::int4;

insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, NULL from generate_series(1, 1500) i;
insert into wisconsin(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, NULL;
vacuum analyze wisconsin;

-- Force index scan
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

-- Main test
select onepercent, unique1 from wisconsin where unique1 = -666;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select onepercent, unique1 from wisconsin where unique1 = -666;

-- Same again, backwards scan
select onepercent, unique1 from wisconsin where unique1 = -666
order by onepercent desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select onepercent, unique1 from wisconsin where unique1 = -666
order by onepercent desc, unique1 desc;

drop index onepercent_idx;

-- Hundred/onepercent, nulls first, repeat last two test cases:
create index onepercent_nulls_first_idx on wisconsin (onepercent desc nulls first, unique1 desc nulls first);

-- Main test
select onepercent, unique1 from wisconsin where unique1 = -666;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select onepercent, unique1 from wisconsin where unique1 = -666;

-- Same again, backwards scan (this variant was broken briefly, when I only
-- had forward scan handling code in _bt_advance_skip_array_increment)
select onepercent, unique1 from wisconsin where unique1 = -666
order by onepercent, unique1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select onepercent, unique1 from wisconsin where unique1 = -666
order by onepercent, unique1;

drop index onepercent_nulls_first_idx;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Four, ten, twenty, unique1 (causes errors about attribute order from
-- _by_preprocess_keys):
create index on wisconsin (four, ten, twenty, unique1);
select four, ten, twenty, unique1
from wisconsin
where
  ten between 1 and 10
  and twenty in (1, 2, 3)
  and unique1 in (84396, 217539, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- master 822 hits
select four, ten, twenty, unique1
from wisconsin
where
  ten between 1 and 10
  and twenty in (1, 2, 3)
  and unique1 in (84396, 217539, 60814);

-- (January 19 2025) Contradictory scan keys should not confuse _bt_preprocess_array_keys
--
-- _bt_preprocess_array_keys shouldn't get confused about contradictory quals
-- with things like a = key and an inequality key on the same attribute.
--
-- Right now, these test cases trick _bt_preprocess_array_keys into throwing
-- errors such as:
-- ERROR:  missing oprcode for skipping equals operator 2437166984

-- Contradictory
select four, ten, unique1
from wisconsin
where
  four = -1 and four between 0 and 3
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  four = -1 and four between 0 and 3
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);

-- Contradictory
select four, ten, unique1
from wisconsin
where
  four between 0 and 3 and four = -1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  four between 0 and 3 and four = -1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);

-- Contradictory
select four, ten, unique1
from wisconsin
where
  four = 4 and four between 0 and 3 and four = -1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  four = 4 and four between 0 and 3 and four = -1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);

-- Partially redundant
select four, ten, unique1
from wisconsin
where
  four = 1 and four between 0 and 3
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  four = 1 and four between 0 and 3
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);

-- Partially redundant
select four, ten, unique1
from wisconsin
where
  four between 0 and 3 and four = 1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin
where
  four between 0 and 3 and four = 1
  and ten between 2 and 15
  and unique1 in (1, 2490, 7777);

-------------------------------------------------------------------------------
-- (July 12) Day after going for drinks with jkatz + company in East Village --
-------------------------------------------------------------------------------
drop index wisconsin_four_ten_twenty_unique1_idx;
create index four_ten_unique1_idx on wisconsin (four, ten, unique1);

-- Missing predicate is in "intermediate" column (ten) here:
-- "#define FORCE_NOSKIP_DEBUG + integer wisconsin table" broke with this
-- query at one point, but it's now fine:
prepare force_noskip_debug as
select four, ten, unique1 from wisconsin where four = 0 and unique1 = 5555;

execute force_noskip_debug;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
execute force_noskip_debug;

-- Consistently prefer bitmap scans:
set enable_indexscan=off;
set enable_sort=on;
set enable_hashagg=off;

drop table if exists small_sales_mdam_paper;
reset client_min_messages;

create unlogged table small_sales_mdam_paper
(
  dept int4,
  sdate date,
  item_class serial,
  store int4,
  item int4,
  total_sales numeric
);
create index small_mdam_idx on small_sales_mdam_paper(dept, sdate, item_class, store);

select setseed(0.5);

insert into small_sales_mdam_paper (dept, sdate, item_class, store, total_sales)
-- total_sales is pretty much just a filler column:
-- omit "item" (which is serial column):
select
  dept,
  '1995-01-01'::date + sdate,
  item_class,
  store,
  (random() * 500.0) as total_sales
from
  -- 10 departments (like in the NOT IN() example, I guess):
  generate_series(1, 10) dept,
  -- 45 days, starting on Jan 1 of 95:
  generate_series(1, 45) sdate,
  -- Arbitrarily assuming 75 total for my standard MDAM table, let's make it 20:
  generate_series(1, 20) item_class,
  -- Arbitrarily assuming 300 total for my standard MDAM table, let's make it 50:
  generate_series(1, 50) store;

vacuum analyze small_sales_mdam_paper;

-- dept range, sdate range, item_class range, store range:
prepare tenth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class between 1 and 15
  and store between 15 and 25
group by sdate, item_class, store;

-- (XXX UPDATE August 6) Doing well on this test hinges upon suppressing the
-- Postgres 17 required-in-opposite-direction new primitive scan logic in
-- _bt_advance_array_keys.
--
-- The issue isn't specific to skip arrays, it's just that it only looks like
-- a regression when comparing skip arrays to similar full index scan.
execute tenth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
execute tenth;
deallocate tenth;

-- dept range, sdate range, item_class range, store range:
prepare eleventh as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class between 1 and 15
  and store = 10 -- Not a range, unlike last time, but otherwise the same
group by sdate, item_class, store;

execute eleventh;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
execute eleventh;
deallocate eleventh;

-- dept range, sdate range, item_class range, store range:
prepare twelfth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class between 1 and 15
  and store = -1 -- Not a range, no matching tuples
group by sdate, item_class, store;

execute twelfth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF)
execute twelfth;
deallocate twelfth;
