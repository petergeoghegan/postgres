set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '0', false);

set client_min_messages=error;
drop table if exists multi_test_small;
drop table if exists duplicate_test_small;
reset client_min_messages;

create unlogged table multi_test_small(
  a int,
  b int
);
create unlogged table duplicate_test_small(dup int4);

create index multi_test_small_idx on multi_test_small(a, b);
create index on duplicate_test_small (dup);

insert into multi_test_small
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

insert into duplicate_test_small
select val from generate_series(1, 18) val,
                generate_series(1,1000) dups_per_val;

vacuum (freeze,analyze) multi_test_small;
vacuum (freeze,analyze) duplicate_test_small; -- Be tidy

-- Simple contradictory
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a = 181;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a = 181;

-- Simple contradictory, but flip order
select *
from multi_test_small
where
  a = 181
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a = 181
  and a in (1, 99, 182, 183, 184);

-- Simple redundant
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a = 182;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '182' (redundant qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a = 182;

-- Simple redundant, but flip order
select *
from multi_test_small
where
  a = 182
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '182' (redundant qual)
select *
from multi_test_small
where
  a = 182
  and a in (1, 99, 182, 183, 184);

---------------------------------
-- '>' operator/strategy tests --
---------------------------------

-- Simple > contradictory
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a > 184;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a > 184;

-- Simple > contradictory, but flip order
select *
from multi_test_small
where
  a > 184
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a > 184
  and a in (1, 99, 182, 183, 184);

-- Simple > redundant
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a > 183;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '184' (redundant qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a > 183;

-- Simple > redundant, but flip order
select *
from multi_test_small
where
  a > 183
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '184' (redundant qual)
select *
from multi_test_small
where
  a > 183
  and a in (1, 99, 182, 183, 184);

----------------------------------
-- '>=' operator/strategy tests --
----------------------------------

-- Simple >= contradictory
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a >= 185;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a >= 185;

-- Simple >= contradictory, but flip order
select *
from multi_test_small
where
  a >= 185
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a >= 185
  and a in (1, 99, 182, 183, 184);

-- Simple >= redundant
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a >= 184;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '184' (redundant qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a >= 184;

-- Simple >= redundant, but flip order
select *
from multi_test_small
where
  a >= 184
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '184' (redundant qual)
select *
from multi_test_small
where
  a >= 184
  and a in (1, 99, 182, 183, 184);

---------------------------------
-- '<' operator/strategy tests --
---------------------------------

-- Simple < contradictory
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a < 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a < 1;

-- Simple < contradictory, but flip order
select *
from multi_test_small
where
  a < 1
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a < 1
  and a in (1, 99, 182, 183, 184);

-- Simple < redundant
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a < 2;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '1' (redundant qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a < 2;

-- Simple < redundant, but flip order
select *
from multi_test_small
where
  a < 2
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '1' (redundant qual)
select *
from multi_test_small
where
  a < 2
  and a in (1, 99, 182, 183, 184);

----------------------------------
-- '<=' operator/strategy tests --
----------------------------------

-- Simple <= contradictory
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a <= 0;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a <= 0;

-- Simple <= contradictory, but flip order
select *
from multi_test_small
where
  a <= 0
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 0 buffer hits (contradictory qual)
select *
from multi_test_small
where
  a <= 0
  and a in (1, 99, 182, 183, 184);

-- Simple <= redundant
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a <= 1;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '1' (redundant qual)
select *
from multi_test_small
where
  a in (1, 99, 182, 183, 184)
  and a <= 1;

-- Simple <= redundant, but flip order
select *
from multi_test_small
where
  a <= 1
  and a in (1, 99, 182, 183, 184);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- only 2 buffer hits for '1' (redundant qual)
select *
from multi_test_small
where
  a <= 1
  and a in (1, 99, 182, 183, 184);

-- Duplicate test, index-only scan
set enable_bitmapscan to off;
set enable_indexonlyscan to on;

-- Warm up test
select count(*), dup from duplicate_test_small
where dup = any (array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 20 hits vs 81 on master
select count(*), dup from duplicate_test_small
where dup = any (array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;

-- Now real tests begin

select count(*), dup from duplicate_test_small
where dup = any (array[( select array_agg(val) from generate_series(1, 40) val)])
and dup > 8 and dup < 11
group by dup order by dup;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 4 hits (include one vm hit)
select count(*), dup from duplicate_test_small
where dup = any (array[( select array_agg(val) from generate_series(1, 40) val)])
and dup > 8 and dup < 11
group by dup order by dup;

-- Variant #1 (changes the order, not the true meaning):
select count(*), dup from duplicate_test_small
where
dup > 8 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)]) and
dup < 11
group by dup order by dup;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 4 hits (include one vm hit)
select count(*), dup from duplicate_test_small
where
dup > 8 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)]) and
dup < 11
group by dup order by dup;

-- Variant #2 (changes the order, not the true meaning):
select count(*), dup from duplicate_test_small
where
dup > 8 and
dup < 11 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 4 hits (include one vm hit)
select count(*), dup from duplicate_test_small
where
dup > 8 and
dup < 11 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;

-- Variant #3 (changes the order, not the true meaning):
select count(*), dup from duplicate_test_small
where
dup < 11 and
dup > 8 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- 4 hits (include one vm hit)
select count(*), dup from duplicate_test_small
where
dup < 11 and
dup > 8 and
dup = any(array[( select array_agg(val) from generate_series(1, 40) val)])
group by dup order by dup;
