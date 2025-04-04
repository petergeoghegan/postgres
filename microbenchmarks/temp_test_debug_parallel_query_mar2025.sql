-- Loosely based on commit 5a1e6df3b8
set debug_parallel_query to regress;
\timing off
\pset pager off

\echo 'Index-only scan, leader participation:'
set enable_bitmapscan to on;
set parallel_leader_participation to off;
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index-only scan, no leader participation:'
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);

\echo 'Index-only scan, standard:'
set debug_parallel_query to off;
set parallel_leader_participation to off;
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
set debug_parallel_query to regress;

\echo 'Index scan, leader participation:'
set enable_indexonlyscan to off;
set enable_indexscan to on;
set enable_bitmapscan to off;
set parallel_leader_participation to on;
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index scan, no leader participation:'
set parallel_leader_participation to off;
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Index scan, standard:'
set debug_parallel_query to off;
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
set debug_parallel_query to regress;

\echo 'Bitmap Index scan, leader participation:'
set enable_indexonlyscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;
set parallel_leader_participation to on;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
\echo 'Bitmap Index scan, no leader participation:'
set parallel_leader_participation to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);

\echo 'Bitmap Index scan, standard:'
set debug_parallel_query to off;
EXPLAIN ANALYZE SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);
SELECT classid, objid FROM pg_depend WHERE classid in (1247, 2618) and objid in (12001, 71310);

-- Melanie test case, which showed "Index Searches: 1" instead of "Index
-- Searches: 10" in nearly-committed "take 2" version of EXPLAIN ANALYZE
-- patch:
-- (Taken from https://www.postgresql.org/message-id/CAAKRu_YjBPfGp85ehY1t9NN%3DR9pB9k%3D6rztaeVkAm-OeTqUK4g%40mail.gmail.com)
reset debug_parallel_query;
drop table if exists tenk1;
drop table if exists tenk2;
CREATE TABLE tenk1 (
    unique1        int4,
    unique2        int4,
    two            int4,
    four        int4,
    ten            int4,
    twenty        int4,
    hundred        int4,
    thousand    int4,
    twothousand    int4,
    fivethous    int4,
    tenthous    int4,
    odd            int4,
    even        int4,
    stringu1    name,
    stringu2    name,
    string4        name
) with (autovacuum_enabled = false);



COPY tenk1 FROM '/mnt/nvme/postgresql/patch/source/src/test/regress/data/tenk.data';



CREATE TABLE tenk2 AS SELECT * FROM tenk1;
CREATE INDEX tenk1_hundred ON tenk1 USING btree(hundred int4_ops);
VACUUM ANALYZE tenk1;
VACUUM ANALYZE tenk2;



set enable_seqscan to off;
set enable_indexscan to off;
set enable_hashjoin to off;
set enable_mergejoin to off;
set enable_material to off;
set parallel_setup_cost=0;
set parallel_tuple_cost=0;
set min_parallel_table_scan_size=0;
set max_parallel_workers_per_gather=2;
set parallel_leader_participation = off;
explain (analyze, costs off, verbose)
    select count(*) from tenk1, tenk2 where tenk1.hundred > 1 and
tenk2.thousand=0;
