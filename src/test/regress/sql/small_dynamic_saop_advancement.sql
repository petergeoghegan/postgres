-- Setup:
drop table if exists btfirst_array_confusion_test;
create unlogged table btfirst_array_confusion_test(
  district int4,
  warehouse int4,
  orderid int4,
  orderline int4
);
create index btfirst_array_confusion_test_idx
            on
            btfirst_array_confusion_test(district, warehouse, orderid, orderline) with (fillfactor = 30);

-- Load:
insert into btfirst_array_confusion_test
select district, warehouse, orderid, orderline
from
  generate_series(1, 2) district,
  generate_series(1, 5) warehouse,
  generate_series(1, 10) orderid,
  generate_series(1, 10) orderline
order by
district, warehouse, orderid, orderline;

-- Index scan without materialization for cursor query:
set enable_seqscan to off;
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_indexscan to on;
set work_mem = 64;
set enable_sort = off;

begin;
declare btfirst_array_confusion_test_cursor cursor for
select * from btfirst_array_confusion_test
where district in (1, 2) and warehouse = 1 and orderid = 9
order by district, warehouse, orderid, orderline;

-- Fetch all 10 orderlines for the first order returned by cursor, so that we
-- read all relevant index tuples from the first leaf page read, and leaf
-- so->currPos.itemIndex right at the end of the page (not one before or one after the
-- end; precisely at the boundaries between two adjoining pages):
fetch forward 10 from btfirst_array_confusion_test_cursor;

-- Fetch 9 orderlines before the last one returned by previous fetch
-- (_bt_first confusion happens here when run against buggy server, as
-- evidenced by this statement returning 10 rows rather than just 9):
fetch backward 10 from btfirst_array_confusion_test_cursor; -- returns an extra row with bug

-- Note: _bt_first becoming confused by the prior fetch statement relies on
-- the fact that the page we almost (but didn't quite) move right from is also
-- the first page visited by the entire top-level index scan for the cursor.
-- This is a necessary condition, since we don't try to call _bt_first again
-- when the scan direction changes unless the scan direction changes on the
-- first page for the top-level scan.

-- Repeat the first fetch, expect the same 10 order + orderline rows as with
-- the first fetch (but don't get them with buggy server with confused array
-- state following previous fetch):
fetch forward 10 from btfirst_array_confusion_test_cursor; -- returns no rows with bug

/* btfirst_array_confusion_test_cursor */ abort;
