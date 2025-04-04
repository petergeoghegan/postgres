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
set client_min_messages=log;

set  enable_seqscan=off;
set  enable_bitmapscan=off;

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

select
  *
from
  fuzz_skip_scan
where
  b <= 4
  and b < 3
  and b < 7
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last
limit 1;

-- Here we should prefer to keep = required scan key in event of redundancy:
select
  *
from
  fuzz_skip_scan
where
a = 1 and
a > 2
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last
limit 1;

-- This needs to put the "=" condition on both a and b first, then put the
-- inequalities after that, without marking the inequalities as required
select
  *
from
  fuzz_skip_scan
where
a = 1 and a > 1 and
b > 1 and b =1
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last
limit 1;

-- Need to reorder Order procs array, too, as shown here:
select
  *
from
  fuzz_skip_scan
where
b = 1 and b > 2
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last
limit 1;

select
  *
from
  fuzz_skip_scan
where
  d = 1324
  and b in (3, 5, 8, 9, 10, 16)
  and a = 5
  and c = 43
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last
limit 1;

select *
from fuzz_skip_scan
where
  d in (3168, 5153, 5380)
  and c = 53
  and b >= 16
  and b >= 13
order by a, b, c, d limit 1;

-- Here we seem to be including non-required mark inequalities on "b", should
-- be plain inequalities marked required but with no redundancy (so 1 "b"
-- key comes at the very end and isn't marked required):
select
  *
from
  fuzz_skip_scan
where
  b >= 14
  and b > 5
  and a < 12
  and a <= 9
order by
  a desc,
  b desc,
  c desc,
  d desc
limit 1;

-- Problem here was with not putting ">=" on "b" at the end:
/*
_bt_preprocess_keys:    so->keyData[0]: [ strategy: = , attno: 1/"a", func: int4eq, flags: [SK_SEARCHARRAY, SK_BT_REQFWD, SK_BT_REQBKWD, SK_BT_SKIP] ]
                          high_compare: [ strategy: <=, attno: 1/"a", func: int4le, flags: [] ]
                        so->keyData[1]: [ strategy: >=, attno: 2/"b", func: int4ge, flags: [SK_BT_REQBKWD] ]
                        so->keyData[2]: [ strategy: = , attno: 2/"b", func: int4eq, flags: [SK_SEARCHARRAY, SK_BT_REQFWD, SK_BT_REQBKWD, SK_BT_SKIP] ]
                           low_compare: [ strategy: >=, attno: 2/"b", func: int4ge, flags: [] ]
                        so->keyData[3]: [ strategy: = , attno: 3/"c", func: int4eq, flags: [SK_BT_REQFWD, SK_BT_REQBKWD] ]
                        so->keyData[4]: [ strategy: = , attno: 4/"d", func: int4eq, flags: [SK_BT_REQFWD, SK_BT_REQBKWD] ]
                        so->keyData[5]: [ strategy: < , attno: 1/"a", func: int4lt, flags: [] ]
*/
select
  *
from
  fuzz_skip_scan
where
  d = 6958
  and c = 23
  and b >= 14
  and b > 5
  and a < 12
  and a <= 9
order by
  a desc,
  b desc,
  c desc,
  d desc
limit 1;

-- Array scan_key offsets get confused with non-required arrays caused by
-- presence of row compares #1:
select
  *
from
  fuzz_skip_scan
where
  c in (21, 97) and (a, b) >= (8, 10) and (a, b) < (10, 0) and a < 17
order by a, b, c, d;

-- Array scan_key offsets get confused with non-required arrays caused by
-- presence of row compares #2:
select
  *
from
  fuzz_skip_scan
where
  d in (1811, 8861) and (a, b) < (13, 16) and c = 63
order by a desc, b desc, c desc, d desc;

-- Row compare _bt_first or not #1
-- (confusion about row type vs scalar inequality redundancies)
select *
from fuzz_skip_scan
where (c, d) > (47, 9012) and c < 103 and (c, d) <= (51, 0)
  and d < 10005
order by
  a desc NULLS last, b desc NULLS last, c desc NULLS last, d desc NULLS last;

-- Row compare _bt_first or not #2
-- (confusion about row type vs scalar inequality redundancies)
select *
from fuzz_skip_scan
where (a, b) >= (14, 19) and (a, b) < (15, 0) and b > - 3 and d <= 2876 and d is not null
order by a NULLS first, b NULLS first, c NULLS first, d NULLS first;

-- More confusion about row type vs scalar inequality redundancies
select *
from fuzz_skip_scan
where
  a is not null and a is not null and (b, c, d) >= (1, 44, 2408) and b < 25 and (b, c, d) <= (2, 0, 0) and c > 8
order by a, b, c, d;

-- Yet more confusion about row type vs scalar inequality redundancies
select *
from fuzz_skip_scan
where (c, d) >= (46, 2455) and (c, d) <= (50, null) and d > 5095
order by a desc, b desc, c desc, d desc;

-- This one hung after I optimistically thought that I might have been able to
-- get away with never refusing to use forcenonrequired mode with a row
-- compare:

select *
from fuzz_skip_scan
where a is not null
  and (b, c, d) >=(12, 71, 3324)
  and (b, c, d) <=(13, 0, null)
  and c < 102
  and d > 7822
order by a desc, b desc, c desc, d desc;

-- ERROR:  pageNum 107 already visited (backwards scan)
select *
from fuzz_skip_scan
where (c, d) >= (63, 377) and (c, d) <= (64, 0) and d >= 8249
order by a desc, b desc, c desc, d desc;

-- 2025-06-24 23:45
--
-- Fuzz testing of array inequalities that cannot be eliminated by
-- preprocessing showed "ERROR:  XX000: missing support function
-- 1(23,822210179) for attribute -32768 of index "fuzz_skip_scan_abcd"" for
-- this:
select
  *
from
  fuzz_skip_scan
where
  d in (748, 1901, 3123, 3183, 3747, 4214, 4366, 4949, 5126, 5376, 5551, 5564, 5795, 6298, 6628, 8375)
  and b in (2, 6, 7, 8, 16)
  and a >= 16
  and a > 11
  and c <= 80
  and b in (1, 2, 4, 5, 6, 7, 9, 11, 12, 13, 14)
order by a desc, b desc, c desc, d desc;

-- TRAP: failed Assert("(cur->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) || so->skipScan"), File: "../source/src/backend/access/nbtree/nbtsearch.c", Line: 1561, PID: 1304852
select *
from fuzz_skip_scan
where (a, b, c, d) > (14, 5, 57, 7865)
  and b > -6
  and c < 104
  and d > -4
order by a desc, b desc, c desc, d desc limit 10;

-- TRAP: failed Assert("!(unmark->sk_flags & SK_ISNULL)"), File: "../source/src/backend/access/nbtree/nbtpreprocesskeys.c", Line: 1846, PID: 1305863
select *
from fuzz_skip_scan
where (a, b, c) >= (-1, null, null)
  and (a, b, c) <= (0, 15, 87)
  and a <= 9
  and b is not null
order by a desc, b desc, c desc, d desc limit 10;

-- ERROR:  pageNum 47 already visited
select *
from fuzz_skip_scan
where
  b in (1, 3, 7, 9, 10, 11, 12, 14, 15, 17, 19)
  and b in (7, 8, 10, 12, 15)
  and b in (5, 11, 15, 17)
order by
  a desc NULLS last,
  b desc NULLS last,
  c desc NULLS last,
  d desc NULLS last;

-- 2025-06-26 20:29
--
-- This test case proves that the "--subkey" behavior within
-- _bt_check_rowcompare definitely isn't safe, even with mature versions of
-- the patch that do all the tricks with NULL tuple values, restoring the
-- original 2006 row compare preprocessing rules, etc.
--
-- 2025-06-26 20:28:23.039 EDT [1386822][client backend] [[unknown]][16/2:0] ERROR:  XX000: pageNum 740 already visited
--
-- XXX UPDATE Actually, it is safe! Just do the same thing in _bt_first! Duh!
select
  *
from
  fuzz_skip_scan
where
  a in (0, 1, 9, 11, 14, 15)
  and (b, d) > (12, 4005)
  and b < 13
  and (b, d) <= (13, null)
order by a desc, b desc, c desc, d desc;


-- 2025-06-29 17:47 Somehow, this query can uses an impliesNN constraint that
-- isn't marked required in either direction by preprocessing...is that
-- correct?
--
-- TRAP: failed Assert("impliesNN == NULL || (impliesNN->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD))"), File: "../source/src/backend/access/nbtree/nbtsearch.c", Line: 1385, PID: 1881061
select
  *
from
  fuzz_skip_scan
where a > 19 and (a, b) >= (19, 19)
order by a desc NULLS last, b desc NULLS last, c desc NULLS last, d desc NULLS last limit 10;

-- 2025-06-29 18:16
--
-- I moved the assertion, and thought I'd come up with a good/valid impliesNN
-- assertion, but then it failed with this other query:
--
-- TRAP: failed Assert("impliesNN->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)"), File: "../source/src/backend/access/nbtree/nbtsearch.c", Line: 1439, PID: 1888198
select *
from fuzz_skip_scan
where
  a < 8
  and (a, b, c) <=(12, 8, 85)
order by a NULLS first, b NULLS first, c NULLS first, d NULLS first limit 10;
