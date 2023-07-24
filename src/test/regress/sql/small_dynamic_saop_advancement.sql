set work_mem='100MB';
set effective_io_concurrency=100;
set effective_cache_size='24GB';
set maintenance_io_concurrency=100;
set random_page_cost=2.0;
set track_io_timing to off;
set enable_seqscan to off;
set client_min_messages=error;
--set log_btree_verbosity=2;
create extension if not exists pageinspect; -- just to have it
reset client_min_messages;

select count(*), two, four from tenk1_dyn_saop
where
two in (0, 1)
and four in (1, 2, 3)
group by
two,
four
order by
two,
four;
select ctid, bar from skippy_tbl where bar in (2,3);

select ctid, bar from skippy_tbl where bar in (2,4);

select * from multi_test where a in (183) and b in (1,2,3,4,5,6,7,8,9,10,11,12);
select ctid, bar from skippy_tbl where bar in (1, 500);

select * from multi_test where a in (182, 183, 184) and b in (1,2);

select * from multi_test where a in (3,4,5) and b < 0;

select * from multi_test where a in (3,4,5) and b < 1;

select count(*), two, four, twenty from tenk1_dyn_saop
where
two in (0, 1)
and four in (1, 2, 3)
and twenty in (1, 2, 14)
group by
two,
four,
twenty
order by
two,
four,
twenty;

select ctid, * from nulls_first
where
  district = 1
  and warehouse = 3
  and orderid is null
  and anotherorderid in (9, 10)
  and orderline in (8, 9, 10, 11);

select ctid, thousand from tenk1_dyn_saop
where
  two in (0, 1) and four = 1 and twenty in (1, 2)
order by two, four, twenty limit 20;

select count(*) from nulls_test where a is NULL and b in (0,1);

select ctid, * from nulls_first where district = 1 and warehouse = 5 and orderid is null and anotherorderid in (11,12) and orderline in (8, 9, 10, 11);

SELECT count(*) FROM functional_dependencies WHERE a IN (1, 51) AND b IN ('1', '2');

select * from multi_test where a in (123, 182, 183, 184) and b > 0;
