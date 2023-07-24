set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;
-- set skipscan_skipsupport_enabled=false;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '3', false);
set statement_timeout='4s';

-----------------------------------------
-- (July 10) Wisconsin numeric variant --
-----------------------------------------

set client_min_messages=error;
drop table if exists wisconsin_numeric;
reset client_min_messages;

create unlogged table wisconsin_numeric
(
unique1 numeric,
unique2 numeric,
two numeric,
four numeric,
ten numeric,
twenty numeric,
onepercent numeric,
tenpercent numeric,
twentypercent numeric,
fiftypercent numeric,
unique3 numeric,
evenonepercent numeric,
oddonepercent numeric,
stringu1 text,
stringu2 text,
string4 text
);

\getenv abs_srcdir PG_ABS_SRCDIR
\set filename :abs_srcdir '/data/wisconsin.csv'
COPY wisconsin_numeric FROM :'filename' with (format csv, encoding 'win1252', header false, null $$$$, quote $$'$$); -- Fix the syntax highlighting: '

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483646,
  2147483646,
  2147483646,
  2147483646,
  2147483646;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
  2147483647,
  2147483647,
  2147483647,
  2147483647,
  2147483647;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647),
(-2147483647);
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select
  5555,
  5555,(-2147483648),
(-2147483648),
(-2147483648),
(-2147483648),
(-2147483648);

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, 2147483647 from generate_series(1, 1500) i;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, (-2147483648)::int4 from generate_series(1, 1500) i;

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, 2147483647;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, (-2147483648)::int4;

insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select 1, 1, 1, 1, 1, 1, NULL from generate_series(1, 1500) i;
insert into wisconsin_numeric(unique1, unique2, two, four, ten, twenty, onepercent)
select -666, 1, 1, 1, 1, 1, NULL;
vacuum analyze wisconsin_numeric;
set enable_bitmapscan to on;
set enable_indexonlyscan to off;
set enable_indexscan to off;

create index numeric_idx_four_ten_twenty_unique1 on wisconsin_numeric (four, ten, twenty, unique1);

-- Simplest version:
select four, ten, twenty, unique1 from wisconsin_numeric where four in (2 , 3) and ten between 3 and 3 and twenty = 3 and unique1 = 84396;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where four in (2 , 3) and ten between 3 and 3 and twenty = 3 and unique1 = 84396;

select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);

drop index numeric_idx_four_ten_twenty_unique1;

-- Four, ten:
create index numeric_four_ten_idx on wisconsin_numeric (four, ten, unique1);

-- (July 18) Make sure that we don't repeatedly access page 1 due to getting
-- confused about -inf value that lands us before the range of the column "ten"
select four, ten, unique1
from wisconsin_numeric
where ten between -10000 and 1 and unique1 = 113
limit 1; -- Just to avoid distraction of other, later pages (just care about page 1)
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select four, ten, unique1
from wisconsin_numeric
where ten between -10000 and 1 and unique1 = 113
limit 1; -- Just to avoid distraction of other, later pages (just care about page 1)

select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten in (2,3) and twenty between 2 and 3 and unique1 in (84396, 60814);

-- Equivalent "range skip array, SAOP" version should also return 2 rows:
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty in (2, 3) and unique1 in (84396, 60814);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF, COSTS OFF)
select four, ten, twenty, unique1 from wisconsin_numeric where ten between 2 and 3 and twenty in (2, 3) and unique1 in (84396, 60814);

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

set client_min_messages=error;
drop table if exists redescend_numeric_test;
reset client_min_messages;
create unlogged table redescend_numeric_test (district numeric, warehouse numeric, orderid numeric, orderline numeric);
create index must_not_full_scan_numeric_idx on redescend_numeric_test (district, warehouse, orderid, orderline) with (fillfactor=30);
insert into redescend_numeric_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 150) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;
-- prewarm
select count(*) from redescend_numeric_test;
vacuum analyze redescend_numeric_test;
---------------------------------------------------------------------------------

insert into redescend_numeric_test
select district, NULL, NULL, NULL
from
  generate_series(1, 3) district,
  generate_series(1, 5) want_five_nulls_per_district;

-- Simplest possible repro:
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from redescend_numeric_test
where district in (1, 2, 3) and warehouse <= 1 and orderid = 1 and orderline = 6
order by district desc, warehouse desc, orderid desc, orderline desc;
