set work_mem='100MB';
set statement_timeout to '2s';
set effective_io_concurrency=100;
set effective_cache_size='24GB';
set maintenance_io_concurrency=100;
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
set log_btree_verbosity=1;
create extension if not exists pageinspect; -- just to have it
reset client_min_messages;

select count(*), two, four from tenk1_dyn_saop
where
two in (0, 1)
and four in (1, 2, 3)
group by
two,
four
order by
two,
four;
select ctid, bar from skippy_tbl where bar in (2,3);

select ctid, bar from skippy_tbl where bar in (2,4);

select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);
select ctid, bar from skippy_tbl where bar in (1, 500);

select * from multi_test where a in (182, 183, 184) and b in (1,2);

select * from multi_test where a in (3,4,5) and b < 0;

select * from multi_test where a in (3,4,5) and b < 1;

select count(*), two, four, twenty from tenk1_dyn_saop
where
two in (0, 1)
and four in (1, 2, 3)
and twenty in (1, 2, 14)
group by
two,
four,
twenty
order by
two,
four,
twenty;

select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);

select ctid, thousand from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (1, 2)
order by two, four, twenty limit 20;

select count(*) from nulls_test where a is NULL and b in (0,1);

select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);

SELECT count(*) FROM functional_dependencies WHERE a IN (1, 51) AND b IN ('1', '2');

select * from multi_test where a in (123, 182, 183, 184) and b > 0;

select ctid, bar from skippy_tbl where bar in (2,4) order by bar desc;

-- Just about the smallest possible stress test that goes over 80 checks of
-- the same tuple for ongoing failures:
with a as (
  select i from generate_series(0, 8) i
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

-- Minimal variant that fails independently due to infinite looping:
with a as (
  select i from generate_series(0, 85) i
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

-- August 26
-- This one remains
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

-- August 25
-- Challenging infinite loop case that made you think about going back to
-- walking tuple in reverse order
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

-- Backwards scan variant
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

select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc;

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

select count(*), a, b, c
from
  functional_dependencies
where a = 25 and c in (0, 1, 2, 3)
group by a, b, c;

select count(*), a, b, c
from
  functional_dependencies
where a = 65 and c in (14, 15)
group by a, b, c;

select count(*), a, b, c
from
  functional_dependencies
  where a in (44,94) and c in (18,19,20)
  group by a, b, c;

SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000)
 ORDER BY thousand;

SELECT thousand, tenthous FROM tenk1_dyn_saop
 WHERE thousand < 2 AND tenthous IN (1001,3000,2001)
 ORDER BY thousand;

set enable_bitmapscan to off;
select count(*), district, orderid
from redescend_test
where district = 2 and orderid in (3, 5)
group by district, orderid;

-- set enable_indexscan to off;
-- set enable_indexonlyscan to off;
-- set enable_seqscan to on;

-- First variant:
select
  ctid,
  two,
  four,
  twenty
from
  tenk1_dyn_saop
where
  two != 0
  and four in (0, 1)
  and twenty in (0, 1)
order by
  two,
  four,
  twenty
limit 20;

-- Second variant (should return same 20 rows as first):
select
  ctid,
  two,
  four,
  twenty
from
  tenk1_dyn_saop
where
  two != 0
  and four in (0, 1)
  and twenty in (1, 2)
order by
  two,
  four,
  twenty
limit 20;

select ctid, *
from functional_dependencies
where
  a in (77, 78)
  and b in ('1', '27')
  and c = any ('{1, 2}');

select ctid, *
from functional_dependencies
where
  a in (76, 77)
  and b in ('1', '27')
  and c = any ('{1, 2}');
