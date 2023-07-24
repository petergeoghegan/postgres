-- encourage use of parallel plans
set parallel_setup_cost=0;
set parallel_tuple_cost=0;
set min_parallel_table_scan_size=0;
set max_parallel_workers_per_gather=4;
set enable_seqscan=off;
set enable_bitmapscan=off;
set enable_indexonlyscan=off;
set enable_indexscan=on;
set log_btree_verbosity=0;

create unlogged table big_parallel_index_scan
(
  c1 int,
  c2 text,
  c3 date,
  c4 varchar(20),
  c5 float
);

insert into big_parallel_index_scan
  select
    x,
    'c2_' || x,
    '2000-01-01'::date + y,
    'xyz',
    1.1
  from
    generate_series(1, 1000000) x,
    generate_series(0,9) y;

create index big_parallel_index_scan_idx on big_parallel_index_scan(c3, c4, c5);

vacuum analyze big_parallel_index_scan;

show port;

-- Parallel:
select
  count(*),
  c3
from
  big_parallel_index_scan
-- Some contiguous stuff, some skipping:
where c3 in (
  '2000-01-01',
  --'2000-01-02',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  --'2000-01-06',
  '2000-01-07',
  --'2000-01-08',
  '2000-01-09',
  --'2000-01-10',
  '1111-11-11'
)
group by c3;

/*
EXPLAIN (ANALYZE, BUFFERS)
select
  count(*),
  c3
from
  big_parallel_index_scan
-- Some contiguous stuff, some skipping:
where c3 in (
  '2000-01-01',
  --'2000-01-02',
  '2000-01-03',
  '2000-01-04',
  '2000-01-05',
  --'2000-01-06',
  '2000-01-07',
  --'2000-01-08',
  '2000-01-09',
  --'2000-01-10',
  '1111-11-11'
)
group by c3;
*/
