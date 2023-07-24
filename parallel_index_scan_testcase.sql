set log_btree_verbosity=1;

set client_min_messages=error;
drop table if exists parallel_index_scan;
reset client_min_messages;

-- encourage use of parallel plans
set parallel_setup_cost=1;
set parallel_tuple_cost=1;
set min_parallel_table_scan_size=1;
set max_parallel_workers_per_gather=2;
set enable_seqscan=off;
set enable_bitmapscan=off;
-- Index-only scan for this
set enable_indexonlyscan=on;

create unlogged table parallel_index_scan
(
  c1 int,
  c2 text,
  c3 date,
  c4 varchar(20),
  c5 float
);

insert into parallel_index_scan
  select
    x,
    'c2_' || x,
    '2000-01-01'::date + y,
    'xyz',
    1.1
  from
    generate_series(1, 100000) x,
    generate_series(0,9) y;

create index parallel_index_scan_idx on parallel_index_scan(c3, c4, c5);

vacuum analyze parallel_index_scan;

show port;

select count(*) as first_count, c3 c3_six_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  '2000-01-07',
  '2000-01-09',
  '1111-11-11'
)
group by c3;
EXPLAIN (ANALYZE, BUFFERS)
select count(*) as first_count, c3 c3_six_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  '2000-01-07',
  '2000-01-09',
  '1111-11-11'
)
group by c3;

select count(*) as second_count, c3 c3_ten_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-02',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  '2000-01-06',
  '2000-01-07',
  '2000-01-08',
  '2000-01-09',
  '2000-01-10'
)
group by c3;
EXPLAIN (ANALYZE, BUFFERS)
select count(*) as second_count, c3 c3_ten_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-02',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  '2000-01-06',
  '2000-01-07',
  '2000-01-08',
  '2000-01-09',
  '2000-01-10'
)
group by c3;

select count(*) as third_count, c3 c3_five_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-03',
  '2000-01-05',
  '2000-01-07',
  '2000-01-09'
)
group by c3;
EXPLAIN (ANALYZE, BUFFERS)
select count(*) as third_count, c3 c3_five_rows
from parallel_index_scan
where c3 in (
  '2000-01-01',
  '2000-01-03',
  '2000-01-05',
  '2000-01-07',
  '2000-01-09'
)
group by c3;
