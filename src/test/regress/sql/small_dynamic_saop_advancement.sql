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
select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '2', false);
set client_min_messages=log;

set  enable_seqscan=off;
set  enable_bitmapscan=off;
set  log_tuple_address=off;

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

-- Simple forwards scan
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c) >= (11, 1, 71) and (a, b, c) <= (18, 1, 1)
order by a, b, c, d;

-- Simple backwards scan
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c) >= (11, 1, 71) and (a, b, c) <= (18, 1, 1)
order by a desc, b desc, c desc, d desc;

-- Proves we need to test "subkey->sk_attno > firstchangingattnum" before
-- moving onto next row compare:
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c) > (2, 18, 28) and (a, b, c) <= (3, 0, null)
order by a desc, b desc, c desc, d desc;

-- Proves we need to test "subkey->sk_attno > firstchangingattnum" on the
-- first row compare member, too
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (c, d) >= (10, 1085) and (c, d) < (12, null)
order by a NULLS first, b NULLS first, c NULLS first, d NULLS first;

-- Proves need for firstnull test
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c) >=(8, 20, 100) and (a, b, c) <=(10, 14, 20)
order by a NULLS first, b NULLS first, c NULLS first, d NULLS first;

-- Proves need for lastnull test
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c, d) >= (3, null, null, null) and (a, b, c, d) <= (4, 11, 56, 2533)
order by a, b, c, d;

-- Proves that SK_ISNULL test is mandatory:
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b, c) > (7, null, 100) and (a, b, c) < (8, 13, 55)
order by a desc NULLS last, b desc NULLS last, c desc NULLS last, d desc NULLS last;

-- Unsure what this does
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b) > (11, 20) and (a, b) < (15, 7)
order by a desc NULLS last, b desc NULLS last, c desc NULLS last, d desc NULLS last;

-- This test case demonstrates that we're leaving money on the table by
-- neglecting to test "subkey->sk_flags & SK_ROW_END" here:
--
-- if (cmpresultlow != 0 || (subkey->sk_flags & SK_ROW_END))
--    {
--       lowsatisfied = _bt_rowcompare_subkey_cmp(subkey,
--                                                cmpresultlow);
--
--       if (!lowsatisfied)
--         break;
--    }
--
-- (Note that it also catches similar omission with cmpresulthigh, on the same
-- single leaf page)
--
-- In short, "pstate.startikey" should be 2 (not 1) on this page:
--
-- _bt_readpage: 🍀  118 with 234 offsets/tuples (rightsib 478, leftsib 426) ⬅️
--  _bt_readpage first: (a, b, c, d)=(11, 6, 71, 813), TID='(930,79)', (nil), from non-pivot non-pivot offnum 234 started page
--  _bt_readpage pstate.startikey: 1, with 2 scan keys
--  _bt_readpage final: (a, b, c, d)=(11, 6, null, null), TID='(759,18)', (nil), from non-pivot offnum 1 did not set so->currPos.moreLeft=false 🟢  ⬅️
--  _bt_readpage stats: currPos.firstItem: 1124, currPos.lastItem: 1357, nmatching: 234 ✅
EXPLAIN (ANALYZE, BUFFERS OFF, TIMING OFF, SUMMARY OFF, COSTS OFF)
select a, b, c
from fuzz_skip_scan
where (a, b) > (9, null) and (a, b) <= (11, 6)
order by a desc NULLS last, b desc NULLS last, c desc NULLS last, d desc NULLS last;
