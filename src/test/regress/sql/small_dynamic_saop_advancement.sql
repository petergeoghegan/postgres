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

-- Establish if this server is master or the patch -- want to skip stress
-- tests if it's the latter
--
-- Reminder: Don't vary the database state between master and patch (just the
-- tests run, which must be read-only)
select (setting = '5432') as testing_patch from pg_settings where name = 'port'
       \gset

-- (April 28 2025) Mark Dilger _bt_check_compare NULL tuple datum handling oversight test case
--
-- Per https://postgr.es/m/CAHgHdKtLFWZcjr87hMH0hYDHgcifu4Tj7iHz-xh8qsJREt5cqA@mail.gmail.com
set client_min_messages=error;
drop table if exists dilger_test;
reset client_min_messages;
CREATE UNLOGGED TABLE dilger_test (
	aid			INTEGER,
	bid			INTEGER,
	ary			FLOAT4[]
);
SELECT setseed(3.0/1024.0);
INSERT INTO dilger_test (aid, bid, ary)
	(SELECT aid, NULL, ary FROM
		(SELECT aid, array_agg(random()) AS ary
			FROM generate_series(1,1000) AS aid, generate_series(1,10)
			GROUP BY aid
		) AS ss
	);
INSERT INTO dilger_test (aid, bid, ary)
	(SELECT aid, drift, array_agg(random()) AS ary
		FROM dilger_test, generate_series(1,1000) AS drift, generate_series(1,10)
		GROUP BY aid, drift
	);
CREATE INDEX dilger_test_idx ON dilger_test USING btree (aid, bid, ary);
VACUUM ANALYZE dilger_test;

-- scan the index, don't overlook bid=NULL tuple:
SET enable_seqscan = off;
SET enable_indexscan = on;
SET enable_bitmapscan = on;
SET enable_indexonlyscan = on;
SELECT COUNT(*)
	FROM dilger_test
	WHERE aid = ANY(ARRAY[1,10,100,1000])
	  AND ary < '{0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0,9,1.0}'::float4[];
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, SUMMARY OFF) -- COSTS OFF added to get stable test output
SELECT COUNT(*)
	FROM dilger_test
	WHERE aid = ANY(ARRAY[1,10,100,1000])
	  AND ary < '{0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0,9,1.0}'::float4[];
