set enable_seqscan = off;
set max_parallel_workers_per_gather=0;
\getenv abs_srcdir PG_ABS_SRCDIR

-- Set log_btree_verbosity to 1 without depending on having that patch
-- applied (HACK, just sets commit_siblings instead when we don't have that
-- patch available):
select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '1', false);
set client_min_messages=debug1;

-- (November 25)
--
-- Here we don't remember the scan's array keys before processing a page, only
-- after processing a page (which is implicit, it's just the scan's current
-- keys).  So when we move the scan backwards we think that the top-level scan
-- should terminate, when in reality it should jump backwards to the leaf page
-- that we last visited.
--
-- This is closely related to the November 24 test, but can fail independently
-- in some way that I don't have the time or patience to pin down right now.
set client_min_messages=error;
drop table if exists backup_wrong_tbl;
reset client_min_messages;

SET enable_seqscan = OFF;
SET enable_bitmapscan = OFF;
set enable_indexonlyscan to off;

create unlogged table backup_wrong_tbl (district int4, warehouse int4, orderid int4, orderline int4);
create index backup_wrong_idx on backup_wrong_tbl (district, warehouse, orderid, orderline);
insert into backup_wrong_tbl
select district, warehouse, orderid, orderline
from
  generate_series(1, 3) district,
  generate_series(1, 2) warehouse,
  generate_series(1, 51) orderid,
  generate_series(1, 10) orderline;

-- prewarm
select count(*) from backup_wrong_tbl;
vacuum analyze backup_wrong_tbl;

SET enable_seqscan = OFF;
SET enable_bitmapscan = OFF;
begin;
declare back_up_terminate_toplevel_wrong cursor for
select * from backup_wrong_tbl
where district in (1, 3) and warehouse in (1,2)
and orderid in (48, 50)
order by district, warehouse, orderid, orderline;
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset

fetch forward 21 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch backward 1 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch forward 12 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch backward 30 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch forward  31 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch backward 32 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch forward  33 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch backward 34 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

fetch forward  35 from back_up_terminate_toplevel_wrong;
\set old_blocks_hit :cur_blocks_hit
select pg_stat_get_xact_blocks_hit('backup_wrong_idx'::regclass) as cur_blocks_hit \gset
select :cur_blocks_hit - :old_blocks_hit bh;

/* back_up_terminate_toplevel_wrong */ commit;
