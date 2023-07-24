set client_min_messages=error;
drop table if exists dec_bug_test;
reset client_min_messages;
create unlogged table dec_bug_test(
  leading_singleval int4,
  second_twovals int4,
  inequal_one_ten_range int4,
  nonrequired_equal_one_ten_range int4
);

create index dec_bug_test_idx on dec_bug_test(leading_singleval, second_twovals, inequal_one_ten_range, nonrequired_equal_one_ten_range) with (fillfactor = 30);

insert into dec_bug_test
select leading_singleval, second_twovals, inequal_one_ten_range, nonrequired_equal_one_ten_range
from
  generate_series(1, 1) leading_singleval,
  generate_series(1, 5) second_twovals,
  generate_series(1, 10) inequal_one_ten_range,
  generate_series(1, 10) nonrequired_equal_one_ten_range
order by
leading_singleval,
second_twovals,
inequal_one_ten_range,
nonrequired_equal_one_ten_range;

-- prewarm
select count(*) from dec_bug_test;
vacuum analyze dec_bug_test;
---------------------------------------------------------------------------------

-- Index scan
set enable_bitmapscan to off;
set enable_indexonlyscan to off;
set enable_seqscan to off;
set enable_indexscan to on;
set skipscan_prefix_cols = 0;

select * from dec_bug_test
where
leading_singleval = 1
  and second_twovals in (1, 2)
  and inequal_one_ten_range <= 10
  and nonrequired_equal_one_ten_range in (1, 2)
order by
  leading_singleval,
  second_twovals,
  inequal_one_ten_range,
  nonrequired_equal_one_ten_range;
