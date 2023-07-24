-- 2025-02-16 -- for some reason skip arrays have wildly worse performance
-- characteristics than equivalent SAOP arrays, though only with parallel
-- scans.  This file is my test case, used to debug that problem.

-- SELECT 2_500_000 AS row_count
SELECT 50_000 AS row_count

\gset

create table parallel_saop_skip_must_agree
(
  c1 int,
  c2 int,
  c3 timestamptz
);

set max_parallel_workers_per_gather=0;

select (select not exists(select * from pg_class where relname = 'parallel_saop_skip_must_agree') or (select (count(*) != :'row_count') from parallel_saop_skip_must_agree where c3 = '2000-01-01')) as load_data
       \gset

\if :load_data
  set client_min_messages=error;
  drop table if exists parallel_saop_skip_must_agree;
  reset client_min_messages;

  create unlogged table parallel_saop_skip_must_agree
  (
    c1 int,
    c2 text,
    c3 timestamptz,
    c4 varchar(20),
    c5 float
  );

  create index parallel_saop_skip_must_agree_idx on parallel_saop_skip_must_agree(c3, c4, c5);

  insert into parallel_saop_skip_must_agree
    select
      x,
      'c2_' || x,
      '2000-01-01'::date + y,
      'xyz' || (x % 5),
      (x % 5)
    from
      generate_series(1, :'row_count') x,
      generate_series(0,30) y;

  -- Now do one with a c1 that's all NULLs:
  insert into parallel_saop_skip_must_agree
  select c1, c2, NULL, c4, c5 from parallel_saop_skip_must_agree where c3 = '2000-01-01';

  vacuum (freeze, analyze) parallel_saop_skip_must_agree;
\endif

DROP EXTENSION IF EXISTS pg_prewarm;
CREATE EXTENSION pg_prewarm;
SELECT pg_prewarm('parallel_saop_skip_must_agree_idx');

show port;

set parallel_setup_cost=000.1;
set parallel_tuple_cost=000.1;
set min_parallel_table_scan_size=1;
set min_parallel_index_scan_size=1;
set enable_seqscan=off;
set enable_bitmapscan=off;
set work_mem='1GB';
-- set enable_indexonlyscan=off;
-- set enable_indexscan=on;

set log_btree_verbosity=1;
set log_array_advance to off;
set parallel_leader_participation=on;
-- set skipscan_prefix_cols = 0;
set max_parallel_workers_per_gather=2;
-- set max_parallel_workers_per_gather=0;

\echo 'SAOP variant of query:'

EXPLAIN (ANALYZE, BUFFERS)
select count(*) as zebra_count,
c3
,c4
,c5
from parallel_saop_skip_must_agree

where

c3 in (
'2000-01-01',
'2000-01-02',
'2000-01-03',
'2000-01-04',
'2000-01-05',
'2000-01-06',
'2000-01-07'
)

and

c4 in ('xyz0', 'xyz2', 'xyz4')

and c5 in (1,2,3,4,5)
group by
c3
, c4
, c5
order by c3, c4, c5
;

\echo 'skip variant of query, which should more or less match, even when parallelism is used:'

EXPLAIN (ANALYZE, BUFFERS)
select count(*) as zebra_count,
c3
,c4
,c5
from parallel_saop_skip_must_agree

where

c3 between '2000-01-01' and '2000-01-07' and

c4 in ('xyz0', 'xyz2', 'xyz4')

and c5 in (1,2,3,4,5)
group by
c3
, c4
, c5
order by c3, c4, c5
;

show max_parallel_workers_per_gather;
