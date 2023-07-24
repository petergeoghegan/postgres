set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;
-- set skipscan_skipsupport_enabled=false;

select set_config((select coalesce((select name from pg_settings where name = 'log_btree_verbosity'), 'commit_siblings')), '3', false);
set statement_timeout='4s';

-- (September 22 2024)
--
-- Sunday after Andrew's birthday.  Test case proved that I was right to be
-- paranoid about reusing NEXTPRIOR flag for backwards and forwards scans --
-- it is indeed subtly broken.
--
-- What if the scan changes direction, and "5 + infinitesimal" becomes
-- "5 - infinitesimal" without our intending it?  That wouldn't be obviously
-- broken in most cases, but it would be broken if the scan happened to have a
-- lower-order SAOP array mixed in.
set work_mem = 64;
set enable_sort = off;

-- (September 22 2024)
--
-- Sunday after Andrew's birthday.  Test case proved that I was right to be
-- paranoid about reusing NEXTPRIOR flag for backwards and forwards scans --
-- it is indeed subtly broken.
--
-- What if the scan changes direction, and "5 + infinitesimal" becomes
-- "5 - infinitesimal" without our intending it?  That wouldn't be obviously
-- broken in most cases, but it would be broken if the scan happened to have a
-- lower-order SAOP array mixed in.
set work_mem = 64;
set enable_sort = off;

-- Index scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;

set client_min_messages=error;
drop table if exists negposinf_duplicate_test;
reset client_min_messages;

create unlogged table negposinf_duplicate_test(dup numeric, otherdup numeric, dups_per_val numeric);
create index negposinf_duplicate_test_idx on negposinf_duplicate_test (dup, otherdup, dups_per_val);
insert into negposinf_duplicate_test(dup, otherdup, dups_per_val)
select val, val, dups_per_val from generate_series(1, 20) val,
                generate_series(1,900) dups_per_val;
vacuum analyze negposinf_duplicate_test; -- Be tidy

begin;
declare negposinf_cursor cursor for
select * from negposinf_duplicate_test
where
  dup between 9 and 11
  -- and otherdup between 9 and 11
  and dups_per_val in (1, 260, 633)
order by dup, otherdup, dups_per_val;

fetch forward 3 from negposinf_cursor;

-- This is how things are now:
--
-- _bt_advance_array_keys, sktrig: 0, pivot tuple: (dup, otherdup, dups_per_val)=(10), 0x7fa334bc7fe0
--
-- - sk: 0, sk_attno: 1, cur_elem:    0, num_elems:   -1, val: 9            <--
-- - sk: 1, sk_attno: 2, cur_elem:    0, num_elems:   -1, val: 9 SK_BT_NEXT
-- - sk: 2, sk_attno: 3, cur_elem:    0, num_elems:    3, val: 1
--
-- + sk: 0, sk_attno: 1, cur_elem:    0, num_elems:   -1, val: 10
-- + sk: 1, sk_attno: 2, cur_elem:    0, num_elems:   -1, val: ????? SK_BT_NEGPOSINF
-- + sk: 2, sk_attno: 3, cur_elem:    0, num_elems:    3, val: 1

/* negposinf_cursor  */ commit;
