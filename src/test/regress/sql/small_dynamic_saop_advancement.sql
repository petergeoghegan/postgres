--set enable_bitmapscan to off;
--set enable_indexonlyscan to off;
--set enable_indexscan to off;

set enable_nestloop to off;
set enable_hashjoin to off;
set enable_material to off;
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
set enable_seqscan to off;
set enable_sort to off;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '0', false);

set client_min_messages=error;
drop table if exists small_test_old;
drop table if exists small_test_new;
reset client_min_messages;

--set client_min_messages=debug1;

create unlogged table small_test_old (district int4, warehouse int4, orderid int4, orderline int4);
create index must_not_full_scan_small_old on small_test_old (district, warehouse, orderid, orderline) with (fillfactor=30);

insert into small_test_old
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;

insert into small_test_old
select district, NULL, NULL, NULL
from
  generate_series(1, 3) district,
  generate_series(1, 5) want_five_nulls_per_district;

-- prewarm
select count(*) from small_test_old;
vacuum analyze small_test_old;
---------------------------------------------------------------------------------

select * from small_test_old where district in (1,2,3) and warehouse > 4 and orderid > 149;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- Old test (must not regress it)
select * from small_test_old where district in (1,2,3) and warehouse > 4 and orderid > 149; -- 54 buffer hits (patch + master)

create unlogged table small_test_new (district int4, warehouse int4, orderid int4, orderline int4);
create index must_not_full_scan_small_new on small_test_new (district, warehouse, orderid, orderline) with (fillfactor=30);

-- Repeat original small_test_old bulk load step for bulk_test_new:
insert into small_test_new
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- Plus add lots of these NULLs, that we need to avoid scanning:
insert into small_test_new
select district, NULL, NULL, NULL
from
  generate_series(1, 2) district,
  generate_series(1, 2000) want_2k_nulls_per_district;

select *
from small_test_new
where
  district in (1, 2, 3)
  and warehouse >= 5
  and orderid >= 150;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- New test
select *
from small_test_new
where
  district in (1, 2, 3)
  and warehouse >= 5
  and orderid >= 150;
