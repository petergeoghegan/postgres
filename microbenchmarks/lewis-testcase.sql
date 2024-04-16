
select '2024-01-01' as testing_date
       \gset

create extension if not exists pg_prewarm;


drop table if exists lewis_testcase;
create unlogged table lewis_testcase as
with generator as (
  select
    generate_series(1, 3000) rownum
)
select
  mod(row_number() over (), 2500) addr_id2500,
  mod(row_number() over (), 50) addr_id0050,
  :'testing_date'::date + (mod(row_number() over (), 2501) / 3)::int4 effective_date,
  lpad(row_number() over ()::text, 10, '0') small_vc,
  rpad('x', 100) padding
from
  generator v1,
  generator v2
limit 250000;
vacuum analyze lewis_testcase;

select pg_prewarm('lewis_testcase');

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '0', false);
set log_btree_verbosity=0;
set client_min_messages=NOTICE;

create index lewis_testcase_i1 on lewis_testcase(effective_date);
select pg_prewarm('lewis_testcase_i1');


EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
        small_vc
from    lewis_testcase
where
        addr_id0050 between 24 and 26 -- make O get skip scan
and     effective_date = :'testing_date'::date;

create index lewis_testcase_i0050 on lewis_testcase(addr_id0050, effective_date);
select pg_prewarm('lewis_testcase_i0050');

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
        small_vc
from    lewis_testcase
where
        addr_id0050 between 24 and 26 -- make O get skip scan
and     effective_date = :'testing_date'::date;

create index lewis_testcase_i2500 on lewis_testcase(addr_id2500, effective_date);
select pg_prewarm('lewis_testcase_i2500');

EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select
        small_vc
from    lewis_testcase
where
        addr_id2500 between 24 and 26 -- make O get range scan
and     effective_date = :'testing_date'::date;
