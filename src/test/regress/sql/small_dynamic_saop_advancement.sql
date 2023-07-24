--set enable_bitmapscan to off;
--set enable_indexonlyscan to off;
--set enable_indexscan to off;

set enable_nestloop to off;
set enable_hashjoin to off;
set enable_material to off;
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
set enable_seqscan to off;
set enable_sort to off;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '3', false);

set client_min_messages=error;
drop table if exists tenk1_dyn_saop;
reset client_min_messages;
\getenv abs_srcdir PG_ABS_SRCDIR
CREATE UNLOGGED TABLE tenk1_dyn_saop (
	unique1		int4,
	unique2		int4,
	two			int4,
	four		int4,
	ten			int4,
	twenty		int4,
	hundred		int4,
	thousand	int4,
	twothousand	int4,
	fivethous	int4,
	tenthous	int4,
	odd			int4,
	even		int4,
	stringu1	name,
	stringu2	name,
	string4		name
);
ALTER TABLE tenk1_dyn_saop SET (autovacuum_enabled=off);

\set filename :abs_srcdir '/data/tenk.data'
COPY tenk1_dyn_saop FROM :'filename';
CREATE INDEX tenk1_dyn_saop_idx_lowcard ON tenk1_dyn_saop (two, four, twenty, hundred);
VACUUM ANALYZE tenk1_dyn_saop;

--set client_min_messages=debug1;

select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two in(-1,0)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two in(-1,0)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

select two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (-1, 0, 1) and four in (1, 2, 3)
  and two in(0, 1, 2)
  and two = (select -1+0.0 offset 0) and two = (select count(*) from pg_operator limit 1)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (-1, 0, 1) and four in (1, 2, 3)
  and two in(0, 1, 2)
  and two = (select -1+0.0 offset 0) and two = (select count(*) from pg_operator limit 1)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22);
