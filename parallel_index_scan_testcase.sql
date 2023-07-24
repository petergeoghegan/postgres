set client_min_messages=error;
drop table if exists parallel_create_index_table;
reset client_min_messages;

-- encourage use of parallel plans
set parallel_setup_cost=0;
set parallel_tuple_cost=0;
set min_parallel_table_scan_size=0;
set max_parallel_workers_per_gather=4;
set enable_seqscan=off;
set enable_bitmapscan=off;
set enable_indexonlyscan=off;
set enable_indexscan=on;

create unlogged table parallel_create_index_table(
  c1 int,
  c2 text,
  c3 date,
  c4 varchar(20),
  c5 float
);

insert into parallel_create_index_table(
  select
    x,
    'c2_' || x,
    to_date('25-09-2015', 'dd-mm-yyyy'),
    'xyz',
    1.1
  from
    generate_series(1, 1000000) x);

insert into parallel_create_index_table(
  select
    x,
    'c2_' || x,
    to_date('25-09-2016', 'dd-mm-yyyy'),
    'xyz',
    1.1
  from
    generate_series(1, 1000000) x);

insert into parallel_create_index_table(
  select
    x,
    'c2_' || x,
    to_date('25-09-2017', 'dd-mm-yyyy'),
    'xyz',
    1.1
  from
    generate_series(1, 1000000) x);

create index parallel_create_index_table_idx on parallel_create_index_table(c3, c4, c5);

vacuum analyze parallel_create_index_table;

select
  count(*),
  c3
from
  parallel_create_index_table
where
  c3 = any (array['2015-09-25'::date, '2017-09-25'::date]) group by c3;
EXPLAIN (ANALYZE, BUFFERS)
select
  count(*),
  c3
from
  parallel_create_index_table
where
  c3 = any (array['2015-09-25'::date, '2017-09-25'::date]) group by c3;
