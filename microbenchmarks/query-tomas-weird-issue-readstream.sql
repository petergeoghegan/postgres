-- Tomas' read stream issue
-- Taken from https://postgr.es/m/8f5d66cf-44e9-40e0-8349-d5590ba8efb4@vondra.me
create extension if not exists pg_prewarm;
create extension if not exists pg_buffercache;
set enable_bitmapscan=off;
-- set effective_io_concurrency=1000;
set enable_seqscan=off;
set max_parallel_workers_per_gather=0;

\echo '#### query that benefits from tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = on ####'
select pg_buffercache_evict_relation('t_readstream');
select pg_prewarm('idx_readstream');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = on;
explain (analyze, timing off)
select *
from t_readstream
where
  a between 16150 and 4540437
order by
  a asc;

\echo '#### query that benefits from tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = off ####'
select pg_buffercache_evict_relation('t_readstream');
select pg_prewarm('idx_readstream');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = off;
explain (analyze, timing off)
select *
from t_readstream
where
  a between 16150 and 4540437
order by
  a asc;

\echo '#### query that is regressed by tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = on ####'
select pg_buffercache_evict_relation('t_tupdistance_new_regress');
select pg_prewarm('t_tupdistance_new_regress_idx');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = on;
explain analyze
select *
from t_tupdistance_new_regress
where a between 9401 and 2271544
order by a desc;

\echo '#### query that is regressed by tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = off ####'
select pg_buffercache_evict_relation('t_tupdistance_new_regress');
select pg_prewarm('t_tupdistance_new_regress_idx');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = off;
explain analyze
select *
from t_tupdistance_new_regress
where a between 9401 and 2271544
order by a desc;

\echo '#### new query that is regressed by tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = on ####'
select pg_buffercache_evict_relation('t_remaining_regression');
select pg_prewarm('t_remaining_regression_idx');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = on;
explain (analyze, TIMING off, costs off)
select * from t_remaining_regression
where a between -2281232 and -19089
order by a asc;

\echo '#### new query that is regressed by tuple distance patch ####'
\echo '#### set enable_indexscan_prefetch = off ####'
select pg_buffercache_evict_relation('t_remaining_regression');
select pg_prewarm('t_remaining_regression_idx');
\! sudo clear_cache.sh
set enable_indexscan_prefetch = off;
explain (analyze, TIMING off, costs off)
select * from t_remaining_regression
where a between -2281232 and -19089
order by a asc;
