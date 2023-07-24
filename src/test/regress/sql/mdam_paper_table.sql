-- See also: original "big" MDAM test table + query file:
-- microbenchmarks/mdam_paper_table.sql

set work_mem='100MB';
set default_statistics_target=2000;
set effective_cache_size='24GB';
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
-- set skipscan_skipsupport_enabled=false;
set vacuum_freeze_min_age = 0;
set cursor_tuple_fraction=1.000;

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

-- skip dept, sdate range, in lists:
prepare first as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  -- dept omitted here
  sdate between '1995-01-01' and '1995-01-05'
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute first;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- win: patch 483, master 2611
execute first;
deallocate first;

-- No skip dept, sdate range, in lists:
prepare second as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept = 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute second;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- close to parity, master only one buffer hit more (48 patch vs 49 master)
execute second;
deallocate second;

-- Backwards scan variant: No skip dept, sdate range, in lists:
prepare third as
select dept, sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept = 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by dept, sdate, item_class, store
order by dept desc, sdate desc, item_class desc, store desc;

execute third;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- backwards scan, master now wins: one buffer hit more (52 patch vs 48 master)
execute third;
deallocate third;

-- No skip dept, sdate range, in lists (similar to last):
prepare fourth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept = 7
  and sdate between '1995-01-15' and '1995-01-30' -- Different date range compared to last
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute fourth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- close to parity, but we lose by a little now: master only one buffer hit more (192 patch vs 182 master)
execute fourth;
deallocate fourth;

-- dept range, sdate range, in lists:
prepare fifth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7 -- only difference with last test is that it was "dept = 7" here instead
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute fifth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- much larger win now (144 patch vs 782 master)
execute fifth;
deallocate fifth;

-- dept range, skip sdate, in lists:
prepare sixth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute sixth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- FIXME master currently beats patch, 1625 hits for patch vs only 1520 for master
execute sixth;
deallocate sixth;

-- dept range, = sdate, in lists:
prepare seventh as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate = '1995-01-10'
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute seventh;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- big win now (36 patch vs 685 master)
execute seventh;
deallocate seventh;


-- dept =, skip sdate, in lists:
prepare eighth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept = 6 -- not a range, unlike last query
  and item_class in (1, 15)
  and store in (15, 25, 45)
group by sdate, item_class, store;

execute eighth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- FIXME master currently beats patch, 543 hits for patch vs only 508 for master
execute eighth;
deallocate eighth;


-- Skip dept, sdate range, in lists:
prepare ninth as
select sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  sdate between '1995-01-01' and '1995-01-05'
  and item_class in (5, 10, 15)
  and store in (25, 45)
group by sdate, item_class, store;

execute ninth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
execute ninth;
deallocate ninth;

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

execute tenth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- Better than I expected: 2063 hits for patch vs 2690 for master
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
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- nice win, 102 for patch vs 723 for master
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
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- nice win, 89 for patch vs 710 for master
execute twelfth;
deallocate twelfth;


-- Backwards scan variant: dept range, sdate range, item_class range, store range:
prepare thirteenth as
select dept, sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class between 1 and 15
  and store between 15 and 25
group by dept, sdate, item_class, store
order by dept desc, sdate desc, item_class desc, store desc;

execute thirteenth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- Better than I expected: 2163 hits for patch vs 2476 for master
execute thirteenth;
deallocate thirteenth;

-- Backwards scan variant: dept range, sdate range, item_class range, store range:
prepare fourteenth as
select dept, sdate, item_class, store, sum(total_sales)
from small_sales_mdam_paper
where
  dept between 5 and 7
  and sdate between '1995-01-01' and '1995-01-05'
  and item_class between 1 and 15
  and store = 10 -- Not a range, unlike last time, but otherwise the same
group by dept, sdate, item_class, store
order by dept desc, sdate desc, item_class desc, store desc;

execute fourteenth;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF) -- good: 202 hits for patch vs 509 for master
execute fourteenth;
deallocate fourteenth;
