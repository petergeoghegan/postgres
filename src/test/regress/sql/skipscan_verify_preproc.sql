set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;

set client_min_messages=error;
drop table if exists scan_key_test;
reset client_min_messages;

create unlogged table scan_key_test(
  a int,
  b int,
  c int,
  d int,
  e int,
  f int,
  g int,
  h int
);

create index scan_key_test_idx on scan_key_test(a, b, c, d, e, f, g, h);

set client_min_messages=log;
set log_btree_verbosity=1;

-- Simplest possible (no skipping, just first column):
select * from scan_key_test where a = 1;

-- Skip column 'a' only:
select * from scan_key_test where b = 1;

-- Skip columns 'a' and 'b' only:
select * from scan_key_test where c = 1;

-- Skip columns 'a' and 'b' only -- carry over c scan key with > strategy:
select * from scan_key_test where c > 1;

-- Skip columns 'a' and 'b' only -- carry over c scan key with >= strategy:
select * from scan_key_test where c >= 1;

-- Skip columns 'a' and 'b' only -- carry over c scan key with < strategy:
select * from scan_key_test where c < 1;

-- Skip columns 'a' only -- carry over b, and carry over c scan key with <= strategy:
select * from scan_key_test where b = 1 and c <= 1;

-- Skip columns 'a' only -- carry over b, and carry over c scan key with > strategy:
select * from scan_key_test where b = 1 and c > 1;

-- Skip columns 'a' only -- carry over b, and carry over c scan key with >= strategy:
select * from scan_key_test where b = 1 and c >= 1;

-- Skip columns 'a' only -- carry over b, and carry over c scan key with < strategy:
select * from scan_key_test where b = 1 and c < 1;

-- Skip columns 'a' only -- carry over b, and carry over c scan key with <= strategy:
select * from scan_key_test where b = 1 and c <= 1;

-- Skip middle column, carry over a and c:
select * from scan_key_test where a = 1 and c = 1;

-- Skip middle columns, carry over a and d:
select * from scan_key_test where a = 1 and d = 1;

-- Skip middle columns, carry over a and e:
select * from scan_key_test where a = 1 and e = 1;

-- Skip middle column, carry over a and c:
select * from scan_key_test where a = 1 and c >= 1;

-- Skip middle columns, carry over a and d:
select * from scan_key_test where a = 1 and d >= 1;

-- Skip middle columns, carry over a and e:
select * from scan_key_test where a = 1 and e >= 1;

-- Skip column 'a' only, range on b:
select * from scan_key_test where b between 1 and 42;

-- Skip column 'a' and 'b' only, range on c:
select * from scan_key_test where c between 1 and 42;

-- Skip column 'b' only, range on c:
select * from scan_key_test where a = 1 and c between 1 and 3;

-- Range of 'a', skip column 'b' only, range on c:
select * from scan_key_test where a between 1 and 3 and c between 1 and 3;

-- Range of 'a', range on 'b', range on c:
select * from scan_key_test where a between 1 and 3 and b between 1 and 3 and c between 1 and 3;

-- Range of 'a', range on 'b', range on c, range on d:
select * from scan_key_test where a between 1 and 3 and b between 1 and 3 and c between 1 and 3 and d between 1 and 3;

-- Range of 'a', range on d:
select * from scan_key_test where a between 1 and 3 and d between 1 and 3;

-- skips 'a', range on b, point on c:
select * from scan_key_test where b between 1 and 3 and c = 1;

-- Range of 'a', range on d, = on e:
select * from scan_key_test where a between 1 and 3 and d between 1 and 3 and e = 1;

-- skips 'a', range on b, point on c, = on e:
select * from scan_key_test where b between 1 and 3 and c = 1 and e = 1;

-- skips 'b' and 'c' only:
select * from scan_key_test where a= 1 and b >= 5 and c >= 4;

-- skips 'b' and 'c':
select * from scan_key_test where a= 1 and c >= 4;

-- skips 'b' only:
select * from scan_key_test where a= 1 and c = 4 and d = 5;

-- skips 'b' range condition only:
 select * from scan_key_test where a= 1 and b between 1 and 2 and c = 4 and d = 5;

-- skips 'b' and e only:
select * from scan_key_test where a= 1 and c = 4 and d = 5 and f = 1;

-- Very complicated variant #1:
select * from scan_key_test where a= 1 and c = 5 and f = 4 and g between 1 and 3 and h = 1;

-- Very complicated variant #2:
select * from scan_key_test where c = 5 and f = 4 and g between 1 and 3 and h = 1;
