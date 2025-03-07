-- Loosely based on commit 5a1e6df3b8
set debug_parallel_query to regress;
\timing off
\pset pager off

\echo 'Index-only scan, leader participation:'
set enable_bitmapscan to on;
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index-only scan, no leader participation:'
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);

\echo 'Index-only scan, standard:'
set debug_parallel_query to off;
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
set debug_parallel_query to regress;

\echo 'Index scan, leader participation:'
set enable_indexonlyscan to off;
set enable_indexscan to on;
set enable_bitmapscan to off;
set parallel_leader_participation to on;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index scan, no leader participation:'
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index scan, standard:'
set debug_parallel_query to off;
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
set debug_parallel_query to regress;

\echo 'Bitmap Index scan, leader participation:'
set enable_indexonlyscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;
set parallel_leader_participation to on;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Bitmap Index scan, no leader participation:'
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);

\echo 'Bitmap Index scan, standard:'
set debug_parallel_query to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
set debug_parallel_query to regress;
