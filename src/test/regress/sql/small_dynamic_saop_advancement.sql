set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;
-- set skipscan_skipsupport_enabled=false;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '1', false);
set statement_timeout='4s';

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

create index name_no_skip_support on tenk1_skipscan (string4, tenthous);

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

set client_min_messages=error;
drop table if exists nulls_first_test_numeric;
reset client_min_messages;

create unlogged table nulls_first_test_numeric(
  a numeric,
  b int
);

create index nulls_first_test_numeric_idx on nulls_first_test_numeric(a nulls first, b);

insert into nulls_first_test_numeric(a, b)
select NULL, i from generate_series(1, 3500) i;
insert into nulls_first_test_numeric(a, b)
select 2147483647, i from generate_series(1, 3500) i;

set enable_indexscan to on;
set enable_bitmapscan to off;

-- Forwards scan
select * from nulls_first_test_numeric where b = 3 order by a nulls first, b;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_first_test_numeric where b = 3 order by a nulls first, b;

-- Backwards scan
select * from nulls_first_test_numeric where b = 3 order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_first_test_numeric where b = 3 order by a desc nulls last, b desc;

-- Now add INT_MIN grouping:
insert into nulls_first_test_numeric(a, b)
select (-2147483648)::int4, i from generate_series(1, 3500) i;

-- Repeat forwards scan
select * from nulls_first_test_numeric where b = 3 order by a nulls first, b;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_first_test_numeric where b = 3 order by a nulls first, b;

-- Repeat backwards scan
select * from nulls_first_test_numeric where b = 3 order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_first_test_numeric where b = 3 order by a desc nulls last, b desc;

------------------------------------------------------------------------
-- (July 2) Alexander Alekseev failing "char" (not char(1)) test case --
------------------------------------------------------------------------
set client_min_messages=error;
drop table if exists alekseev_test;
reset client_min_messages;
create table alekseev_test(c "char", n bigint);

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

-- (February 22)
--
-- Assertion failure due to non-required scan key not starting from the first
-- array element (for current scan direction) after _bt_preprocess_keys was
-- done:
prepare functional_dependencies_norequired_no_first_elem as
select count(*), a, b, c
from
  functional_dependencies
where
  a in (1,2) and c in (1,2,3) and c = 2
group by a, b, c;

-- Original assertion failure:
--
-- TRAP: failed Assert("_bt_verify_arrays_bt_first(scan, dir)"), File: "../source/src/backend/access/nbtree/nbtutils.c", Line: 2776, PID: 513412
-- [0x556451e17798] _bt_preprocess_keys: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtutils.c:2776
-- [0x556451e0e646] _bt_first: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtsearch.c:1181
-- [0x556451e0b0da] btgettuple: /mnt/nvme/postgresql/patch/build_meson_dc/../source/src/backend/access/nbtree/nbtree.c:290
--
-- Fix for this was to move the assertion into _bt_array_keys_remain, where
-- _bt_preprocess_keys incremental advancement cannot break things.
execute functional_dependencies_norequired_no_first_elem;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_no_first_elem;
deallocate functional_dependencies_norequired_no_first_elem;

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
prepare functional_dependencies_norequired_one_random as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c;

execute functional_dependencies_norequired_one_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one_random;
deallocate functional_dependencies_norequired_one_random;

-- Test case for missing key column in predicate -- must skip 14 and match on
-- 15 here, slightly trickier
prepare functional_dependencies_norequired_two_random as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 65 and c in (14, 15)
group by a, b, c;

execute functional_dependencies_norequired_two_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two_random;
deallocate functional_dependencies_norequired_two_random;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three_random as
select count(*), a, b, c
from
  functional_dependencies_random
  where a in (44,94) and c in (18,19,20)
  group by a, b, c;

execute functional_dependencies_norequired_three_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three_random;
deallocate functional_dependencies_norequired_three_random;

prepare functional_dependencies_norequired_four_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_four_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four_random;
deallocate functional_dependencies_norequired_four_random;

prepare functional_dependencies_norequired_five_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c;

execute functional_dependencies_norequired_five_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five_random;
deallocate functional_dependencies_norequired_five_random;

prepare functional_dependencies_norequired_six_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c;

execute functional_dependencies_norequired_six_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six_random;
deallocate functional_dependencies_norequired_six_random;

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
prepare functional_dependencies_norequired_one_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_one_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_one_desc_random;
deallocate functional_dependencies_norequired_one_desc_random;

prepare functional_dependencies_norequired_two_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
where a = 65 and c in (14, 15)
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_two_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_two_desc_random;
deallocate functional_dependencies_norequired_two_desc_random;

-- Skip to different parts of index for each of 44, 94:
prepare functional_dependencies_norequired_three_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
  where a in (44,94) and c in (18,19,20)
  group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_three_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_three_desc_random;
deallocate functional_dependencies_norequired_three_desc_random;

prepare functional_dependencies_norequired_four_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 51])
  and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_four_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_four_desc_random;
deallocate functional_dependencies_norequired_four_desc_random;

prepare functional_dependencies_norequired_five_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_five_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_five_desc_random;
deallocate functional_dependencies_norequired_five_desc_random;

prepare functional_dependencies_norequired_six_desc_random as
select count(*), a, b, c
from
  functional_dependencies_random
where
  a in (1, 2, 51, 52) and b = '1'
group by a, b, c
order by a desc, b desc, c desc;

execute functional_dependencies_norequired_six_desc_random;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute functional_dependencies_norequired_six_desc_random;
deallocate functional_dependencies_norequired_six_desc_random;

set client_min_messages=error;
drop table if exists wisconsin_numeric;
reset client_min_messages;

-----------------------------------------
-- (July 10) Wisconsin numeric variant --
-----------------------------------------

create unlogged table wisconsin_numeric
(
unique1 numeric,
unique2 numeric,
two numeric,
four numeric,
ten numeric,
twenty numeric,
onepercent numeric,
tenpercent numeric,
twentypercent numeric,
fiftypercent numeric,
unique3 numeric,
evenonepercent numeric,
oddonepercent numeric,
stringu1 text,
stringu2 text,
string4 text
);

\set filename :abs_srcdir '/data/wisconsin.csv'
COPY wisconsin_numeric FROM :'filename' with (format csv, encoding 'win1252', header false, null $$$$, quote $$'$$); -- Fix the syntax highlighting: '

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483646,
  2147483646,
  2147483646,
  2147483646,
  2147483646;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483647,
  2147483647,
  2147483647,
  2147483647,
  2147483647;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647);
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
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

-- Ten:
create index numeric_ten_idx on wisconsin_numeric (ten, unique1);

-- Range instead of skip attribute on "ten":
select ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555;

-- Range instead of skip attribute on "ten", backwards scan:
set enable_bitmapscan to off;
set enable_indexscan to on;
select ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555 order by ten desc, unique1 desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555 order by ten desc, unique1 desc;
set enable_bitmapscan to on;
set enable_indexscan to off;



drop index numeric_ten_idx;

-- Four, ten:
create index numeric_four_ten_idx on wisconsin_numeric (four, ten, unique1);

-- Point lookup, skips one col (four), range on other col after that (ten):
select four, ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, unique1 from wisconsin_numeric where ten between -10000 and 4 and unique1 = 5555;

drop index numeric_four_ten_idx;

set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, 2147483647 from generate_series(1, 1500) i;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, (-2147483648)::int4 from generate_series(1, 1500) i;

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, 2147483647;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, (-2147483648)::int4;

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, NULL from generate_series(1, 1500) i;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, NULL;
vacuum analyze wisconsin_numeric;
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

-- Four, ten, twenty, unique1 (causes errors about attribute order from
-- _by_preprocess_keys):
create index on wisconsin_numeric (four, ten, twenty, unique1);

-- Simplest version:
select four, ten, twenty, unique1 from wisconsin_numeric where four in (2 , 3) and ten between 3 and 3 and twenty = 3 and unique1 = 84396;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where four in (2 , 3) and ten between 3 and 3 and twenty = 3 and unique1 = 84396;

-- Simplest version of other, similar bug:
select four, ten, twenty, unique1 from wisconsin_numeric where four between 2 and 3 and ten between 2 and 3 and twenty = 3 and unique1 = 84396;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where four between 2 and 3 and ten between 2 and 3 and twenty = 3 and unique1 = 84396;

-- SAOP-only version should return 2 rows:
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty in (2, 3) and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty in (2, 3) and unique1 in (84396, 60814);

-- Equivalent "SAOP, range skip array" version should also return 2 rows:
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);

-- Equivalent "range skip array, SAOP" version should also return 2 rows:
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty in (2, 3) and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty in (2, 3) and unique1 in (84396, 60814);

-- Equivalent "range skip array, range skip array" version should also return 2 rows:
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty between 2 and 3 and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty between 2 and 3 and unique1 in (84396, 60814);

-- (July 12) One of the last failing test cases when adding support for
-- non-skip-support opclasses.
--
-- This is based on int tests from Postgres 17 SAOP test suite, which failed
-- when I forced nbtree to not use skip support as a simple smoke test
set client_min_messages=error;
drop table if exists redescend_numeric_test;
reset client_min_messages;
create unlogged table redescend_numeric_test (district numeric, warehouse numeric, orderid numeric, orderline numeric);
create index must_not_full_scan_numeric on redescend_numeric_test (district, warehouse, orderid, orderline) with (fillfactor=30);
insert into redescend_numeric_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- prewarm
select count(*) from redescend_numeric_test;
vacuum analyze redescend_numeric_test;
---------------------------------------------------------------------------------

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

set client_min_messages=error;
drop table if exists redescend_numeric_test;
reset client_min_messages;
create unlogged table redescend_numeric_test (district numeric, warehouse numeric, orderid numeric, orderline numeric);
create index must_not_full_scan_numeric_idx on redescend_numeric_test (district, warehouse, orderid, orderline) with (fillfactor=30);
insert into redescend_numeric_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- prewarm
select count(*) from redescend_numeric_test;
vacuum analyze redescend_numeric_test;
---------------------------------------------------------------------------------

insert into redescend_numeric_test
select district, NULL, NULL, NULL
from
  generate_series(1, 3) district,
  generate_series(1, 5) want_five_nulls_per_district;

-- Simplest possible repro:
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test where district in (1,2,3) and warehouse > 4 and orderid > 149;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test where district in (1,2,3) and warehouse > 4 and orderid > 149; -- 54 buffer hits (patch + master)

select * from redescend_numeric_test where district in (1,2,3) and warehouse = 5 and orderid > 149;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test where district in (1,2,3) and warehouse = 5 and orderid > 149; -- 6 buffer hits (patch + master)

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse < 2 and orderid < 2 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse < 2 and orderid < 2 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid <= 1 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid <= 1 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid in (0, 1) and orderline >= any ('{6,7,8}')
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid in (0, 1) and orderline >= any ('{6,7,8}')
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid <= 1 and orderline in (-1, 6, 8, 1000)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid <= 1 and orderline in (-1, 6, 8, 1000)
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid < 2 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid < 2 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;

select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse < 2 and orderid <= 1 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse < 2 and orderid <= 1 and orderline in (6, 7, 8)
order by district desc, warehouse desc, orderid desc, orderline desc;
