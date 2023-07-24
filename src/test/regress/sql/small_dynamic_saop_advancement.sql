--set enable_bitmapscan to off;
--set enable_indexonlyscan to off;
--set enable_indexscan to off;
set enable_seqscan=off;
set log_btree_verbosity=2;
--set client_min_messages=debug1;

-- Index-only scan:
set enable_bitmapscan to off;
set enable_indexonlyscan to on;
set enable_indexscan to off;

-- Minimal backwards scan confusion test case:
select * from nulls_test where a in (183,307) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (183,307) order by a desc nulls last, b desc;

-- Original NYC backwards scan confusion test case from big tests:
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (1,2,350,359,360) and b in (-1,-2,1) order by a desc nulls last, b desc; -- 4 or 5 (depending on if you count the VM or not) buffer accesses

select * from multi_test where a in (182, 183, 184) and b in (1,2) order by a desc, b desc;
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from multi_test where a in (182, 183, 184) and b in (1,2) order by a desc, b desc;

-- Same again, but forwards scan -- this one currently gets 6 hits total,
-- which is kinda weird because high key (184,-inf) is considered ahead of scan
-- keys, whereas first non-pivot tuple on sibling page (184,*) is considered
-- before scan keys:
select * from nulls_test where a in (183,307);
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY OFF)
select * from nulls_test where a in (183,307);
