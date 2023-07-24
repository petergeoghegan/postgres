drop table if exists opfamily_test;
drop operator family if exists test_family using btree cascade;

-- Create opfamily with two generic opclasses (both incomplete, just for
-- brevity):
create operator family test_family using btree;
create operator class test_int4_ops for type int4 using btree family test_family as
  operator 3 = (int4,int4),
  operator 5 > (int4,int4),
  function 1 btint4cmp(int4,int4);
create operator class test_int8_ops for type int8 using btree family test_family as
  operator 3 = (int8,int8),
  operator 5 > (int8,int8),
  function 1 btint8cmp(int8,int8);

-- Create cross-type operator and support function that will allow
-- _bt_preprocess_keys to fail to notice redundant inequality scan keys later on:
alter operator family test_family using btree add
  operator 3 = (int8, int4);
alter operator family test_family using btree add
  function 1 btint84cmp(int8, int4);

create table opfamily_test(foo int8);
create index on opfamily_test(foo test_int8_ops);
insert into opfamily_test select i from generate_series(1,1000) i;

-- Actually returns a single tuple (with the value 90), which is of course
-- wrong (actually, an assertion failure happens on assert-enabled builds):
select foo as should_return_no_rows_wrong
from opfamily_test
where foo > 99::int8 and foo = 90;

-- This variant uses an index scan that gives the correct answer, though:
select foo as will_return_no_rows_correct
from opfamily_test
where foo > 99::int8 and foo = 90::int8;
