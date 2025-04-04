set work_mem='100MB';
set effective_cache_size='24GB';
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
-- set skipscan_skipsupport_enabled=false;
-- set skipscan_iprefix_enabled=false;
-- set skipscan_prefix_cols=0;
set vacuum_freeze_min_age = 0;
set cursor_tuple_fraction=1.000;
create extension if not exists pageinspect; -- just to have it
-- set statement_timeout='4s';
reset client_min_messages;

-- Set log_btree_verbosity to 1 without depending on having that patch
-- applied (HACK, just sets commit_siblings instead when we don't have that
-- patch available):
select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '1', false);

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

-- Quick sanity check, to make it obvious when you forgot to initdb correctly:
-- Shows the available skip support routines in the database
select
  amp.oid as skip_proc_oid,
  amp.amproc::regproc as proc,
  opf.opfname as opfamily_name,
  opc.opcname as opclass_name,
  opc.opcintype::regtype as opcintype
from pg_am as am
join pg_opclass as opc on opc.opcmethod = am.oid
join pg_opfamily as opf on opc.opcfamily = opf.oid
join pg_amproc as amp on amp.amprocfamily = opf.oid and
    amp.amproclefttype = opc.opcintype and amp.amprocnum = 6
where am.amname = 'btree'
order by 1, 2, 3, 4;

--
-- Heikki CREATE INDEX regression stress test query (miniaturized) --
--
-- Test case taken from: https://postgr.es/m/aa55adf3-6466-4324-92e6-5ef54e7c3918@iki.fi
--
-- set enable_seqscan=off; set max_parallel_workers_per_gather=0;

-- Setup:
set client_min_messages=error;
drop table if exists heikki_skiptest_small;
reset client_min_messages;

-- First do retail insert version of his query, where suffix truncation is
-- effective:
create unlogged table heikki_skiptest_small (a int, b int);
create index heikki_skiptest_small_idx on heikki_skiptest_small (a, b);

insert into heikki_skiptest_small
select g / 10 as a, g % 10 as b
from generate_series(1, 10_000) g;
vacuum freeze heikki_skiptest_small;

-- Okay, now the actual adversarial case, which requires that suffix
-- truncation wasn't very effective -- REINDEX to get that:
reindex index heikki_skiptest_small_idx;

-- Now repeat exactly the same queries as first time around:

-- Backwards:
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select a, b
from heikki_skiptest_small
where b = 1
order by a desc, b desc
limit 1 offset 20_000;
