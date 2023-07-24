-- based on https://postgr.es/m/527571eb98b9bed54c8cadb261c14795@oss.nttdata.com

-- HOWTO get number of distinct values for a given page range (blkno 1 - 1000 here):
/*
select i, count(distinct (regexp_match(data, '\(([0-9]+),'))[1])
from (select i from
    generate_series(1, 1000) i) ii,
lateral bt_page_items('t_idx', i)
where dead is not null
group by i;
*/

-- SET skipscan_prefix_cols=0;

\pset pager off
set client_min_messages='notice';
set enable_seqscan=off;
\set numrows 1_000_000

-- This makes quite a big improvement with "SELECT * FROM test_fifteen WHERE
-- id2 = 501", other marginal queries:

-- set skipscan_skipsupport_enabled to off;

create unlogged table test_one -- fake
(
  c1 int
);
select (select not exists(select * from pg_class where relname = 'test_one') or (select count(*) != :'numrows' from test_one)) as load_data
       \gset

\if :load_data
  -- One
  \set rows_per_group 1
  DROP TABLE IF EXISTS test_one;
  CREATE unlogged TABLE test_one (id1 int, id2 int);
  CREATE INDEX t_one_idx on test_one (id1, id2);
  insert into test_one (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  VACUUM (FREEZE, VERBOSE, ANALYZE) test_one;

  -- One sequential
  \set rows_per_group 1
  DROP TABLE IF EXISTS test_one_sequential;
  CREATE unlogged TABLE test_one_sequential (id1 int, id2 int);
  CREATE INDEX t_one_sequential_idx on test_one_sequential (id1, id2);
  insert into test_one_sequential (
    select
      i,
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  VACUUM (FREEZE, VERBOSE, ANALYZE) test_one_sequential;


  -- Five
  \set rows_per_group 5
  DROP TABLE IF EXISTS test_five;
  CREATE unlogged TABLE test_five (id1 int, id2 int);
  CREATE INDEX t_five_idx on test_five (id1, id2);
  insert into test_five (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  VACUUM (FREEZE, VERBOSE, ANALYZE) test_five;

  -- Fifteen
  \set rows_per_group 15
  DROP TABLE IF EXISTS test_fifteen;
  CREATE unlogged TABLE test_fifteen (id1 int, id2 int);
  CREATE INDEX t_fifteen_idx on test_fifteen (id1, id2);
  insert into test_fifteen (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  VACUUM (FREEZE, VERBOSE, ANALYZE) test_fifteen;

  -- Seventeen
  \set rows_per_group 17
  DROP TABLE IF EXISTS test_seventeen;
  CREATE unlogged TABLE test_seventeen (id1 int, id2 int);
  CREATE INDEX t_seventeen_idx on test_seventeen (id1, id2);
  insert into test_seventeen (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  VACUUM (FREEZE, VERBOSE, ANALYZE) test_seventeen;

  -- Twenty
  \set rows_per_group 20
  drop table if exists test_twenty;
  create unlogged table test_twenty (id1 int, id2 int);
  create index t_twenty_idx on test_twenty (id1, id2);
  insert into test_twenty (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  vacuum (freeze, verbose, analyze) test_twenty;

  -- Twenty-five
  \set rows_per_group 25
  drop table if exists test_twentyfive;
  create unlogged table test_twentyfive (id1 int, id2 int);
  create index t_twentyfive_idx on test_twentyfive (id1, id2);
  insert into test_twentyfive (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  vacuum (freeze, verbose, analyze) test_twentyfive;

  -- fifty
  \set rows_per_group 50
  drop table if exists test_fifty;
  create unlogged table test_fifty (id1 int, id2 int);
  create index t_fifty_idx on test_fifty (id1, id2);
  insert into test_fifty (
    select
      abs(hashint4(i::int4)),
      abs(hashint4(i::int4) # hashint4(j::int4)) % :numrows
    from
      generate_series(1, (:numrows / :rows_per_group)) s(i),
      generate_series(1, :rows_per_group) j);
  vacuum (freeze, verbose, analyze) test_fifty;
\endif

---------
-- One --
---------
\echo 'SELECT * FROM test_one WHERE id2 = 1:'
SELECT * FROM test_one WHERE id2 = 1;
SELECT * FROM test_one WHERE id2 = 1;
SELECT * FROM test_one WHERE id2 = 1;
SELECT * FROM test_one WHERE id2 = 1;
SELECT * FROM test_one WHERE id2 = 1;
SELECT * FROM test_one WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one WHERE id2 = 1;


\echo 'SELECT * FROM test_one WHERE id2 = 501:'
SELECT * FROM test_one WHERE id2 = 501;
SELECT * FROM test_one WHERE id2 = 501;
SELECT * FROM test_one WHERE id2 = 501;
SELECT * FROM test_one WHERE id2 = 501;
SELECT * FROM test_one WHERE id2 = 501;
SELECT * FROM test_one WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one WHERE id2 = 501;


\echo 'SELECT * FROM test_one WHERE id2 = 900:'
SELECT * FROM test_one WHERE id2 = 900;
SELECT * FROM test_one WHERE id2 = 900;
SELECT * FROM test_one WHERE id2 = 900;
SELECT * FROM test_one WHERE id2 = 900;
SELECT * FROM test_one WHERE id2 = 900;
SELECT * FROM test_one WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one WHERE id2 = 900;


\echo 'SELECT * FROM test_one WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one WHERE id2 IN (0, 1, 900);

---------------------
-- One, sequential --
---------------------
\echo 'SELECT * FROM test_one_sequential WHERE id2 = 1:'
SELECT * FROM test_one_sequential WHERE id2 = 1;
SELECT * FROM test_one_sequential WHERE id2 = 1;
SELECT * FROM test_one_sequential WHERE id2 = 1;
SELECT * FROM test_one_sequential WHERE id2 = 1;
SELECT * FROM test_one_sequential WHERE id2 = 1;
SELECT * FROM test_one_sequential WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one_sequential WHERE id2 = 1;


\echo 'SELECT * FROM test_one_sequential WHERE id2 = 501:'
SELECT * FROM test_one_sequential WHERE id2 = 501;
SELECT * FROM test_one_sequential WHERE id2 = 501;
SELECT * FROM test_one_sequential WHERE id2 = 501;
SELECT * FROM test_one_sequential WHERE id2 = 501;
SELECT * FROM test_one_sequential WHERE id2 = 501;
SELECT * FROM test_one_sequential WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one_sequential WHERE id2 = 501;


\echo 'SELECT * FROM test_one_sequential WHERE id2 = 900:'
SELECT * FROM test_one_sequential WHERE id2 = 900;
SELECT * FROM test_one_sequential WHERE id2 = 900;
SELECT * FROM test_one_sequential WHERE id2 = 900;
SELECT * FROM test_one_sequential WHERE id2 = 900;
SELECT * FROM test_one_sequential WHERE id2 = 900;
SELECT * FROM test_one_sequential WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one_sequential WHERE id2 = 900;


\echo 'SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_one_sequential WHERE id2 IN (0, 1, 900);

----------
-- Five --
----------
\echo 'SELECT * FROM test_five WHERE id2 = 1:'
SELECT * FROM test_five WHERE id2 = 1;
SELECT * FROM test_five WHERE id2 = 1;
SELECT * FROM test_five WHERE id2 = 1;
SELECT * FROM test_five WHERE id2 = 1;
SELECT * FROM test_five WHERE id2 = 1;
SELECT * FROM test_five WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_five WHERE id2 = 1;


\echo 'SELECT * FROM test_five WHERE id2 = 501:'
SELECT * FROM test_five WHERE id2 = 501;
SELECT * FROM test_five WHERE id2 = 501;
SELECT * FROM test_five WHERE id2 = 501;
SELECT * FROM test_five WHERE id2 = 501;
SELECT * FROM test_five WHERE id2 = 501;
SELECT * FROM test_five WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_five WHERE id2 = 501;


\echo 'SELECT * FROM test_five WHERE id2 = 900:'
SELECT * FROM test_five WHERE id2 = 900;
SELECT * FROM test_five WHERE id2 = 900;
SELECT * FROM test_five WHERE id2 = 900;
SELECT * FROM test_five WHERE id2 = 900;
SELECT * FROM test_five WHERE id2 = 900;
SELECT * FROM test_five WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_five WHERE id2 = 900;


\echo 'SELECT * FROM test_five WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
SELECT * FROM test_five WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_five WHERE id2 IN (0, 1, 900);

-------------
-- Fifteen --
-------------
\echo 'SELECT * FROM test_fifteen WHERE id2 = 1:'
SELECT * FROM test_fifteen WHERE id2 = 1;
SELECT * FROM test_fifteen WHERE id2 = 1;
SELECT * FROM test_fifteen WHERE id2 = 1;
SELECT * FROM test_fifteen WHERE id2 = 1;
SELECT * FROM test_fifteen WHERE id2 = 1;
SELECT * FROM test_fifteen WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifteen WHERE id2 = 1;


\echo 'SELECT * FROM test_fifteen WHERE id2 = 501:'
SELECT * FROM test_fifteen WHERE id2 = 501;
SELECT * FROM test_fifteen WHERE id2 = 501;
SELECT * FROM test_fifteen WHERE id2 = 501;
SELECT * FROM test_fifteen WHERE id2 = 501;
SELECT * FROM test_fifteen WHERE id2 = 501;
SELECT * FROM test_fifteen WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifteen WHERE id2 = 501;


\echo 'SELECT * FROM test_fifteen WHERE id2 = 900:'
SELECT * FROM test_fifteen WHERE id2 = 900;
SELECT * FROM test_fifteen WHERE id2 = 900;
SELECT * FROM test_fifteen WHERE id2 = 900;
SELECT * FROM test_fifteen WHERE id2 = 900;
SELECT * FROM test_fifteen WHERE id2 = 900;
SELECT * FROM test_fifteen WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifteen WHERE id2 = 900;


\echo 'SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifteen WHERE id2 IN (0, 1, 900);

---------------
-- Seventeen --
---------------
\echo 'SELECT * FROM test_seventeen WHERE id2 = 1:'
SELECT * FROM test_seventeen WHERE id2 = 1;
SELECT * FROM test_seventeen WHERE id2 = 1;
SELECT * FROM test_seventeen WHERE id2 = 1;
SELECT * FROM test_seventeen WHERE id2 = 1;
SELECT * FROM test_seventeen WHERE id2 = 1;
SELECT * FROM test_seventeen WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_seventeen WHERE id2 = 1;


\echo 'SELECT * FROM test_seventeen WHERE id2 = 501:'
SELECT * FROM test_seventeen WHERE id2 = 501;
SELECT * FROM test_seventeen WHERE id2 = 501;
SELECT * FROM test_seventeen WHERE id2 = 501;
SELECT * FROM test_seventeen WHERE id2 = 501;
SELECT * FROM test_seventeen WHERE id2 = 501;
SELECT * FROM test_seventeen WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_seventeen WHERE id2 = 501;


\echo 'SELECT * FROM test_seventeen WHERE id2 = 900:'
SELECT * FROM test_seventeen WHERE id2 = 900;
SELECT * FROM test_seventeen WHERE id2 = 900;
SELECT * FROM test_seventeen WHERE id2 = 900;
SELECT * FROM test_seventeen WHERE id2 = 900;
SELECT * FROM test_seventeen WHERE id2 = 900;
SELECT * FROM test_seventeen WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_seventeen WHERE id2 = 900;


\echo 'SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_seventeen WHERE id2 IN (0, 1, 900);

------------
-- Twenty --
------------
\echo 'SELECT * FROM test_twenty WHERE id2 = 1:'
SELECT * FROM test_twenty WHERE id2 = 1;
SELECT * FROM test_twenty WHERE id2 = 1;
SELECT * FROM test_twenty WHERE id2 = 1;
SELECT * FROM test_twenty WHERE id2 = 1;
SELECT * FROM test_twenty WHERE id2 = 1;
SELECT * FROM test_twenty WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twenty WHERE id2 = 1;


\echo 'SELECT * FROM test_twenty WHERE id2 = 501:'
SELECT * FROM test_twenty WHERE id2 = 501;
SELECT * FROM test_twenty WHERE id2 = 501;
SELECT * FROM test_twenty WHERE id2 = 501;
SELECT * FROM test_twenty WHERE id2 = 501;
SELECT * FROM test_twenty WHERE id2 = 501;
SELECT * FROM test_twenty WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twenty WHERE id2 = 501;


\echo 'SELECT * FROM test_twenty WHERE id2 = 900:'
SELECT * FROM test_twenty WHERE id2 = 900;
SELECT * FROM test_twenty WHERE id2 = 900;
SELECT * FROM test_twenty WHERE id2 = 900;
SELECT * FROM test_twenty WHERE id2 = 900;
SELECT * FROM test_twenty WHERE id2 = 900;
SELECT * FROM test_twenty WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twenty WHERE id2 = 900;


\echo 'SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twenty WHERE id2 IN (0, 1, 900);

-----------------
-- Twenty-five --
-----------------
\echo 'SELECT * FROM test_twentyfive WHERE id2 = 1:'
SELECT * FROM test_twentyfive WHERE id2 = 1;
SELECT * FROM test_twentyfive WHERE id2 = 1;
SELECT * FROM test_twentyfive WHERE id2 = 1;
SELECT * FROM test_twentyfive WHERE id2 = 1;
SELECT * FROM test_twentyfive WHERE id2 = 1;
SELECT * FROM test_twentyfive WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twentyfive WHERE id2 = 1;


\echo 'SELECT * FROM test_twentyfive WHERE id2 = 501:'
SELECT * FROM test_twentyfive WHERE id2 = 501;
SELECT * FROM test_twentyfive WHERE id2 = 501;
SELECT * FROM test_twentyfive WHERE id2 = 501;
SELECT * FROM test_twentyfive WHERE id2 = 501;
SELECT * FROM test_twentyfive WHERE id2 = 501;
SELECT * FROM test_twentyfive WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twentyfive WHERE id2 = 501;


\echo 'SELECT * FROM test_twentyfive WHERE id2 = 900:'
SELECT * FROM test_twentyfive WHERE id2 = 900;
SELECT * FROM test_twentyfive WHERE id2 = 900;
SELECT * FROM test_twentyfive WHERE id2 = 900;
SELECT * FROM test_twentyfive WHERE id2 = 900;
SELECT * FROM test_twentyfive WHERE id2 = 900;
SELECT * FROM test_twentyfive WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twentyfive WHERE id2 = 900;


\echo 'SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_twentyfive WHERE id2 IN (0, 1, 900);

-----------
-- Fifty --
-----------
\echo 'SELECT * FROM test_fifty WHERE id2 = 1:'
SELECT * FROM test_fifty WHERE id2 = 1;
SELECT * FROM test_fifty WHERE id2 = 1;
SELECT * FROM test_fifty WHERE id2 = 1;
SELECT * FROM test_fifty WHERE id2 = 1;
SELECT * FROM test_fifty WHERE id2 = 1;
SELECT * FROM test_fifty WHERE id2 = 1;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifty WHERE id2 = 1;


\echo 'SELECT * FROM test_fifty WHERE id2 = 501:'
SELECT * FROM test_fifty WHERE id2 = 501;
SELECT * FROM test_fifty WHERE id2 = 501;
SELECT * FROM test_fifty WHERE id2 = 501;
SELECT * FROM test_fifty WHERE id2 = 501;
SELECT * FROM test_fifty WHERE id2 = 501;
SELECT * FROM test_fifty WHERE id2 = 501;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifty WHERE id2 = 501;


\echo 'SELECT * FROM test_fifty WHERE id2 = 900:'
SELECT * FROM test_fifty WHERE id2 = 900;
SELECT * FROM test_fifty WHERE id2 = 900;
SELECT * FROM test_fifty WHERE id2 = 900;
SELECT * FROM test_fifty WHERE id2 = 900;
SELECT * FROM test_fifty WHERE id2 = 900;
SELECT * FROM test_fifty WHERE id2 = 900;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifty WHERE id2 = 900;


\echo 'SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900):'
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM test_fifty WHERE id2 IN (0, 1, 900);
