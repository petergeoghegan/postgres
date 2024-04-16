set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to off;
set enable_bitmapscan to on;

set client_min_messages=error;
drop table if exists scan_key_test;
drop table if exists scan_key_unsupported_test;
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

-- Skip columns 'a' and 'b' only -- convert c to range skip array:
select * from scan_key_test where c > 1;

-- Skip columns 'a' and 'b' only -- convert c to range skip array:
select * from scan_key_test where c >= 1;

-- Skip columns 'a' and 'b' only -- convert c to range skip array:
select * from scan_key_test where c < 1;

------------------------
-- a, b, c test cases --
------------------------

-- True skip columns 'a' only -- carry over b, and convert c to range skip array:
select * from scan_key_test where b = 1 and c <= 1;

-- True skip columns 'a' only -- carry over b, and convert c to range skip array:
select * from scan_key_test where b = 1 and c > 1;

-- True skip columns 'a' only -- carry over b, and convert c to range skip array:
select * from scan_key_test where b = 1 and c >= 1;

-- True skip columns 'a' only -- carry over b, and convert c to range skip array:
select * from scan_key_test where b = 1 and c < 1;

-- True skip columns 'a' only -- carry over b, and convert c to range skip array:
select * from scan_key_test where b = 1 and c <= 1;

------------------------------------------
-- a, b, c test cases, but now with GUC --
------------------------------------------

-- Similar to last few tests, in that "a" always gets a skip scan attribute.
-- But dissimilar in that we keep inequalities as-is:
set skipscan_prefix_cols = 1;

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

-- Increment GUC to 2:
set skipscan_prefix_cols = 2;
-- Same queries again, but (since b=1 in all cases) get the same output scan
-- keys as previous skipscan_prefix_cols=1 variants of these same tests
-- (the right to put a skip scan key on "b" isn't what's missing here):
select * from scan_key_test where b = 1 and c <= 1;
select * from scan_key_test where b = 1 and c > 1;
select * from scan_key_test where b = 1 and c >= 1;
select * from scan_key_test where b = 1 and c < 1;
select * from scan_key_test where b = 1 and c <= 1;

-- Increment GUC to 3:
set skipscan_prefix_cols = 3;
-- Same queries again, but (since c is attnum 3) we get the maximum number of
-- possibly-useful skip arrays -- even the "c" inequality scan keys get
-- converted to range style skip scan keys (we're back to the GUC-less
-- behavior, which is the default behavior for the patch):
select * from scan_key_test where b = 1 and c <= 1;
select * from scan_key_test where b = 1 and c > 1;
select * from scan_key_test where b = 1 and c >= 1;
select * from scan_key_test where b = 1 and c < 1;
select * from scan_key_test where b = 1 and c <= 1;

-- Increment GUC to 4:
set skipscan_prefix_cols = 4;
-- Let's be absolutely sure that further increases make no difference to the
-- behavior of skip array preprocessing:
select * from scan_key_test where b = 1 and c <= 1;
select * from scan_key_test where b = 1 and c > 1;
select * from scan_key_test where b = 1 and c >= 1;
select * from scan_key_test where b = 1 and c < 1;
select * from scan_key_test where b = 1 and c <= 1;

-- Disable all skipping, even on attribute a/attnum 1:
set skipscan_prefix_cols = 0;
select * from scan_key_test where b = 1 and c <= 1;
select * from scan_key_test where b = 1 and c > 1;
select * from scan_key_test where b = 1 and c >= 1;
select * from scan_key_test where b = 1 and c < 1;
select * from scan_key_test where b = 1 and c <= 1;

reset skipscan_prefix_cols;

----------------------
-- end of GUC tests --
----------------------

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

create unlogged table scan_key_unsupported_test(
  a int,
  b int,
  c text,
  d int,
  e text,
  f int,
  g text,
  h int
);

create index scan_key_unsupported_test_idx on scan_key_unsupported_test(a, b, c, d, e, f, g, h);

-- Skip columns 'a' and 'b' successfully, in spite of not supporting text for
-- skipping:
select * from scan_key_unsupported_test where c = 'foo';

-- Skip columns 'a' and 'b' and 'd' successfully, in spite of not supporting text for
-- skipping:
select * from scan_key_unsupported_test where c = 'foo' and e = 'bar';

-- Skip columns 'a' and 'b' successfully, but not c or even d (d is int but
-- doesn't matter because it's after c, a text column):
select * from scan_key_unsupported_test where e = 'bar';

-- Skip columns 'a' successfully, but not c or even d (d is int but
-- doesn't matter because it's after c, a text column):
select * from scan_key_unsupported_test where b = 22 and e = 'bar';

-- Skip columns 'a' and 'b' successfully, in spite of not supporting text for
-- skipping, but no range skip scan key for c:
select * from scan_key_unsupported_test where c between 'a' and 'z';

-- Skip columns 'a' and 'b' successfully, in spite of not supporting text for
-- skipping, but no range skip scan key for c, nor a skip scan key for d:
select * from scan_key_unsupported_test where c between 'a' and 'z' and e = 'bar';
