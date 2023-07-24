Motivating examples and measurement rig for the index scan cost model change
(see REPORT.md for the full study; raw timing/cost JSONL and the builds stay
under claude_work/costmodel on this machine).  The scripts carry absolute
paths for this machine, like the other microbenchmarks.
The debug_index_cost_model and debug_disable_index GUCs that rig.py,
models.json and the illustrate_*.py scripts set exist only in the
scratch worktree build described in REPORT.md (branch costmodel-exp), not
in any binary built from this tree.

# Published random_page_cost complaints (../random_page_cost_cases.py)

`microbenchmarks/random_page_cost_cases.py [--case ...]` builds each case from
its `random_page_cost_cases_<case>.sql` (usable by hand with `psql -f`,
`-v nrows=N` for the size) on the patch cluster (`--master` or `--conninfo` for
another server) and prints, under random_page_cost 4 and 1.1 and
effective_io_concurrency 16 and 1, the EXPLAIN (ANALYZE, BUFFERS) output of
the plan the planner picks, then a summary with the cost and the forced time
of each plan alternative (the other index hidden with `indisvalid = false` in
a rolled-back transaction, or enable_* switched).  All cached, best of
`--runs`.  The eic = 1 rows are the stock costing (the discount is off; the
stock build picks the same plans).

**lateral**, the shayon.dev post: a LATERAL join, 500 incomplete queue rows per
parent row in id order, over a table whose statistics predate the rows'
completion, so the planner believes nearly every row matches.  The inner side
is a LATERAL subquery with its own LIMIT, planned once with the outer
reference as a Param, so its index scan is costed at loop_count 1 and gets the
discount although it runs once per outer row.

| random_page_cost | eic | plan chosen | ms | pkey walk cost / ms | composite index cost / ms |
|---|---|---|---|---|---|
| 4.0 | 16 | composite index | 0.46 | 1137 / 57 | 717 / 0.5 |
| 4.0 | 1 | pkey walk, 299,200 rows removed per loop | 57 | 1137 / 57 | 1975 / 0.5 |
| 1.1 | 16 | composite index | 0.46 | 882 / 57 | 528 / 0.5 |
| 1.1 | 1 | composite index | 0.45 | 882 / 57 | 570 / 0.5 |

(The post: pkey walk with 146,339 rows removed per loop, 11 s on its cloud
storage; the composite index at random_page_cost 1.1, 1.5 ms.)

**orderby_limit**, pgsql-performance 2011 "Query with order by and limit is
very slow - wrong index used": `WHERE source_id = 'wbc' ORDER BY id LIMIT 200`
with a (source_id, id) index, the matching 30% of the rows sitting at the end
of the table.  The statistics are right; the LIMIT costing assumes the
matches are spread over the id range, so the pkey walk looks cheap.  The
poster's random_page_cost 2.0 and 1.5 did not change the plan (124 s); the
discount does at 4.

| random_page_cost | eic | plan chosen | ms | pkey walk cost / ms | (source_id, id) cost / ms |
|---|---|---|---|---|---|
| 4.0 | 16 | (source_id, id) index | 0.03 | 40.3 / 58 | 24.4 / 0.03 |
| 4.0 | 1 | pkey walk, 700,000 rows removed | 56 | 40.3 / 56 | 56.0 / 0.03 |
| 1.1 | 16 | (source_id, id) index | 0.03 | 35.1 / 55 | 20.1 / 0.03 |
| 1.1 | 1 | (source_id, id) index | 0.03 | 35.1 / 55 | 21.1 / 0.03 |

**bitmapand**, pgsql-hackers 2017 "Bitmap scan is undercosted?": two
single-column indexes on columns that are not independent (every num = 1 row
has flag = true), `WHERE num = 1 AND flag = true`.  The planner expects the
BitmapAnd to cut the heap rows tenfold (the row estimate, as Tom Lane said in
the thread) and costs that plan at under half the plain index scan; the
discount does not change the choice, and on this machine the BitmapAnd is the
faster plan anyway (the thread had the index scan 2.7x faster).  A negative
case: nothing in the cost change should flip it.

| random_page_cost | eic | plan chosen | ms | BitmapAnd cost / ms | index scan on i1 cost / ms |
|---|---|---|---|---|---|
| 4.0 | 16 | BitmapAnd | 58 | 20662 / 61 | 46836 / 87 |
| 4.0 | 1 | BitmapAnd | 58 | 33422 / 58 | 87806 / 84 |
| 1.1 | 16 | BitmapAnd | 56 | 17159 / 56 | 41731 / 86 |
| 1.1 | 1 | BitmapAnd | 56 | 17585 / 56 | 43096 / 82 |

# Plan-family thresholds on a small table (../random_page_cost_sweep.py)

`microbenchmarks/random_page_cost_sweep.py [--time]` is the guard from Franck
Pachot's "PostgreSQL with modern storage: what about a lower random_page_cost?":
demo(id, val) with 100k rows in 541 pages, val random, id correlated; for X
from 1% to 100% of the range it records the plan family for `val < X` and
`id < X` under each setting.  In the post the default went bitmap -> seq at
38%, and random_page_cost 1.1 produced a plain index scan from 17% to 56% that
read 30x the seq scan's buffers.  With the cost change the thresholds at
random_page_cost 4 are the stock ones, and no plain-scan region appears even at
1.1 (the buffer lookups of the uncorrelated case keep it above the bitmap
scan):

| setting | val < X: bitmap | plain index | seq | id < X: index | seq |
|---|---|---|---|---|---|
| rpc 4, eic 16 (the change) | 1-41% | - | 42-100% | 1-52% | 53-100% |
| rpc 4, eic 1 (stock) | 1-41% | - | 42-100% | 1-52% | 53-100% |
| rpc 1.1, eic 16 | 1-53% | - | 54-100% | 1-68% | 69-100% |
| rpc 1.1, eic 1 | 1-53% | - | 54-100% | 1-68% | 69-100% |

Forced plans for `val < X`, cached, best of 3 (ms): 5%: bitmap 0.62, index
0.85, seq 3.3; 20%: 1.6, 3.2, 3.6; 40%: 2.8, 6.5, 3.9; 60%: 3.9, 9.7, 4.1.
The bitmap scan is the fastest family through 60% on this table, so the plain
index scan region of the post's 1.1 setting would have been the slow choice.

# Plain index scan vs bitmap heap scan + Sort, with index prefetching

Setup: `illustrate_setup.sql` (three tables, 1.4-1.6 GB of heap each) and `illustrate_setup_ios.sql`.  The index-prefetch branch plus the cost model commits, release build, 16 GB shared_buffers, io_uring, io_combine_limit=16, stock planner settings (random_page_cost=4).  Local NVMe (Samsung 980 PRO).  EXPLAIN (ANALYZE, BUFFERS, IO), best of 2 or 3 runs.

Cache states: "90% cached" = the table prewarmed into shared_buffers, then every heap page whose block number hashes to 0 mod 10 evicted with pg_buffercache_evict() and the table's files dropped from the OS page cache, so the scan finds 90% of its pages in shared_buffers and reads the other 10% as isolated single-page device reads: the uniform-miss model behind the cache-hit assumption of random_page_cost.  "fully cached" = everything in shared_buffers.  "heap uncached" = heap evicted from shared_buffers and from the OS page cache; index and visibility map warm in every state.

## Summary (execution time, ms)

| query | plan | 90% cached, eic=16 | 90% cached, eic=100 | fully cached | heap uncached, eic=16 | heap uncached, eic=100 |
|---|---|---|---|---|---|---|
| t_dups | Index Scan | 101 | 102 | 88 | 361 | 233 |
| t_dups | Sort over Bitmap Heap Scan | 272 | 276 | 261 | 323 | 296 |
| t_runs | Index Scan | 100 | 100 | 85 | 115 | 108 |
| t_runs | Sort over Bitmap Heap Scan | 272 | 271 | 281 | 279 | 279 |
| t_sparse | Index Scan | 23 | 23 | 7 | 289 | 129 |
| t_sparse | Sort over Bitmap Heap Scan | 45 | 33 | 18 | 244 | 103 |

The stock planner picks the Sort over Bitmap Heap Scan for all three queries (costs in the outputs).  With the cost model change (random-access penalty of prefetched heap pages divided by effective_io_concurrency, both scan types) the planner picks the Index Scan for all three, at eic=16 and at eic=100 (`results/illustration/choices_patch.txt`).

## t_dups: clustered duplicates: 64 rows per grp on ~1.3 adjacent pages, groups in random order (the t_randomized shape); 1M rows, 32k of 182k heap pages

```sql
SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp;
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Index Scan using t_dups_grp on t_dups  (cost=0.43..751029.27 rows=1021111 width=117) (actual time=0.011..89.397 rows=1000000.00 loops=1)
  Index Cond: ((grp >= 40001) AND (grp <= 55625))
  Index Searches: 1
  Prefetch: avg=172.47 max=255 capacity=272
  I/O: count=3052 waits=8 size=1.06 in-progress=15.75
  Buffers: shared hit=31171 read=3229
Planning Time: 0.058 ms
Execution Time: 101.263 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Sort  (cost=312890.09..315442.86 rows=1021111 width=117) (actual time=226.724..248.353 rows=1000000.00 loops=1)
  Sort Key: grp
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=29613 read=3229
  ->  Bitmap Heap Scan on t_dups  (cost=13838.82..210974.49 rows=1021111 width=117) (actual time=19.594..105.991 rows=1000000.00 loops=1)
        Recheck Cond: ((grp >= 40001) AND (grp <= 55625))
        Heap Blocks: exact=32017
        Prefetch: avg=123.27 max=127 capacity=272
        I/O: count=3017 waits=7 size=1.07 in-progress=12.35
        Buffers: shared hit=29613 read=3229
        ->  Bitmap Index Scan on t_dups_grp  (cost=0.00..13583.55 rows=1021111 width=0) (actual time=15.355..15.356 rows=1000000.00 loops=1)
              Index Cond: ((grp >= 40001) AND (grp <= 55625))
              Index Searches: 1
              Buffers: shared hit=825
Planning Time: 0.060 ms
Execution Time: 272.226 ms
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Index Scan using t_dups_grp on t_dups  (cost=0.43..751029.27 rows=1021111 width=117) (actual time=0.011..90.876 rows=1000000.00 loops=1)
  Index Cond: ((grp >= 40001) AND (grp <= 55625))
  Index Searches: 1
  Prefetch: avg=221.02 max=255 capacity=1616
  I/O: count=3053 waits=8 size=1.06 in-progress=20.76
  Buffers: shared hit=31171 read=3229
Planning Time: 0.052 ms
Execution Time: 102.447 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Sort  (cost=312890.09..315442.86 rows=1021111 width=117) (actual time=226.584..247.917 rows=1000000.00 loops=1)
  Sort Key: grp
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=29613 read=3229
  ->  Bitmap Heap Scan on t_dups  (cost=13838.82..210974.49 rows=1021111 width=117) (actual time=19.950..106.716 rows=1000000.00 loops=1)
        Recheck Cond: ((grp >= 40001) AND (grp <= 55625))
        Heap Blocks: exact=32017
        Prefetch: avg=124.93 max=127 capacity=1616
        I/O: count=3017 waits=7 size=1.07 in-progress=12.61
        Buffers: shared hit=29613 read=3229
        ->  Bitmap Index Scan on t_dups_grp  (cost=0.00..13583.55 rows=1021111 width=0) (actual time=15.721..15.721 rows=1000000.00 loops=1)
              Index Cond: ((grp >= 40001) AND (grp <= 55625))
              Index Searches: 1
              Buffers: shared hit=825
Planning Time: 0.064 ms
Execution Time: 276.210 ms
```

### Index Scan, fully cached

```
Index Scan using t_dups_grp on t_dups  (cost=0.43..751029.27 rows=1021111 width=117) (actual time=0.020..77.154 rows=1000000.00 loops=1)
  Index Cond: ((grp >= 40001) AND (grp <= 55625))
  Index Searches: 1
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=34400
Planning:
  Buffers: shared hit=57
Planning Time: 0.175 ms
Execution Time: 88.124 ms
```

### Sort over Bitmap Heap Scan, fully cached

```
Sort  (cost=312890.09..315442.86 rows=1021111 width=117) (actual time=213.882..235.692 rows=1000000.00 loops=1)
  Sort Key: grp
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=32842
  ->  Bitmap Heap Scan on t_dups  (cost=13838.82..210974.49 rows=1021111 width=117) (actual time=19.253..95.275 rows=1000000.00 loops=1)
        Recheck Cond: ((grp >= 40001) AND (grp <= 55625))
        Heap Blocks: exact=32017
        Prefetch: avg=1.00 max=1 capacity=1616
        Buffers: shared hit=32842
        ->  Bitmap Index Scan on t_dups_grp  (cost=0.00..13583.55 rows=1021111 width=0) (actual time=15.161..15.161 rows=1000000.00 loops=1)
              Index Cond: ((grp >= 40001) AND (grp <= 55625))
              Index Searches: 1
              Buffers: shared hit=825
Planning Time: 0.064 ms
Execution Time: 260.508 ms
```

### Index Scan, heap uncached, effective_io_concurrency=16

```
Index Scan using t_dups_grp on t_dups  (cost=0.43..751029.27 rows=1021111 width=117) (actual time=0.172..344.319 rows=1000000.00 loops=1)
  Index Cond: ((grp >= 40001) AND (grp <= 55625))
  Index Searches: 1
  Prefetch: avg=34.94 max=45 capacity=272
  I/O: count=15589 waits=2027 size=2.05 in-progress=15.99
  Buffers: shared hit=2383 read=32017
Planning Time: 0.057 ms
Execution Time: 360.846 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=16

```
Sort  (cost=312890.09..315442.86 rows=1021111 width=117) (actual time=272.336..297.083 rows=1000000.00 loops=1)
  Sort Key: grp
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=825 read=32017
  ->  Bitmap Heap Scan on t_dups  (cost=13838.82..210974.49 rows=1021111 width=117) (actual time=20.022..145.796 rows=1000000.00 loops=1)
        Recheck Cond: ((grp >= 40001) AND (grp <= 55625))
        Heap Blocks: exact=32017
        Prefetch: avg=40.74 max=62 capacity=272
        I/O: count=12859 waits=843 size=2.49 in-progress=15.98
        Buffers: shared hit=825 read=32017
        ->  Bitmap Index Scan on t_dups_grp  (cost=0.00..13583.55 rows=1021111 width=0) (actual time=15.669..15.670 rows=1000000.00 loops=1)
              Index Cond: ((grp >= 40001) AND (grp <= 55625))
              Index Searches: 1
              Buffers: shared hit=825
Planning Time: 0.069 ms
Execution Time: 323.278 ms
```

### Index Scan, heap uncached, effective_io_concurrency=100

```
Index Scan using t_dups_grp on t_dups  (cost=0.43..751029.27 rows=1021111 width=117) (actual time=0.164..213.912 rows=1000000.00 loops=1)
  Index Cond: ((grp >= 40001) AND (grp <= 55625))
  Index Searches: 1
  Prefetch: avg=214.90 max=231 capacity=1616
  I/O: count=15594 waits=73 size=2.05 in-progress=99.60
  Buffers: shared hit=2383 read=32017
Planning Time: 0.051 ms
Execution Time: 233.105 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Sort  (cost=312890.09..315442.86 rows=1021111 width=117) (actual time=249.724..271.230 rows=1000000.00 loops=1)
  Sort Key: grp
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=825 read=32017
  ->  Bitmap Heap Scan on t_dups  (cost=13838.82..210974.49 rows=1021111 width=117) (actual time=19.861..125.718 rows=1000000.00 loops=1)
        Recheck Cond: ((grp >= 40001) AND (grp <= 55625))
        Heap Blocks: exact=32017
        Prefetch: avg=125.75 max=127 capacity=1616
        I/O: count=12859 waits=7 size=2.49 in-progress=50.65
        Buffers: shared hit=825 read=32017
        ->  Bitmap Index Scan on t_dups_grp  (cost=0.00..13583.55 rows=1021111 width=0) (actual time=15.534..15.534 rows=1000000.00 loops=1)
              Index Cond: ((grp >= 40001) AND (grp <= 55625))
              Index Searches: 1
              Buffers: shared hit=825
Planning Time: 0.060 ms
Execution Time: 296.208 ms
```

## t_runs: 4-page runs in random order, unique key, correlation ~0; 1M rows, 20k of 182k heap pages

```sql
SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id;
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Index Scan using t_runs_id on t_runs  (cost=0.43..758322.27 rows=1003799 width=117) (actual time=0.013..88.093 rows=1000000.00 loops=1)
  Index Cond: ((id >= 2500001) AND (id <= 3500000))
  Index Searches: 1
  Prefetch: avg=113.05 max=127 capacity=272
  I/O: count=2154 waits=7 size=1.08 in-progress=11.44
  Buffers: shared hit=23549 read=2334
Planning Time: 0.056 ms
Execution Time: 99.507 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Sort  (cost=318241.26..320750.76 rows=1003799 width=117) (actual time=228.176..247.427 rows=1000000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=23061 read=2334
  ->  Bitmap Heap Scan on t_runs  (cost=21301.37..218177.36 rows=1003799 width=117) (actual time=24.133..97.856 rows=1000000.00 loops=1)
        Recheck Cond: ((id >= 2500001) AND (id <= 3500000))
        Heap Blocks: exact=22660
        Prefetch: avg=85.05 max=127 capacity=272
        I/O: count=2147 waits=7 size=1.09 in-progress=9.05
        Buffers: shared hit=23061 read=2334
        ->  Bitmap Index Scan on t_runs_id  (cost=0.00..21050.43 rows=1003799 width=0) (actual time=21.219..21.220 rows=1000000.00 loops=1)
              Index Cond: ((id >= 2500001) AND (id <= 3500000))
              Index Searches: 1
              Buffers: shared hit=2735
Planning Time: 0.060 ms
Execution Time: 271.806 ms
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Index Scan using t_runs_id on t_runs  (cost=0.43..758322.27 rows=1003799 width=117) (actual time=0.013..88.936 rows=1000000.00 loops=1)
  Index Cond: ((id >= 2500001) AND (id <= 3500000))
  Index Searches: 1
  Prefetch: avg=114.23 max=127 capacity=1616
  I/O: count=2154 waits=7 size=1.08 in-progress=11.59
  Buffers: shared hit=23549 read=2334
Planning Time: 0.059 ms
Execution Time: 100.492 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Sort  (cost=318241.26..320750.76 rows=1003799 width=117) (actual time=228.063..247.139 rows=1000000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=23061 read=2334
  ->  Bitmap Heap Scan on t_runs  (cost=21301.37..218177.36 rows=1003799 width=117) (actual time=24.331..98.831 rows=1000000.00 loops=1)
        Recheck Cond: ((id >= 2500001) AND (id <= 3500000))
        Heap Blocks: exact=22660
        Prefetch: avg=60.64 max=63 capacity=1616
        I/O: count=2147 waits=6 size=1.09 in-progress=6.54
        Buffers: shared hit=23061 read=2334
        ->  Bitmap Index Scan on t_runs_id  (cost=0.00..21050.43 rows=1003799 width=0) (actual time=21.436..21.437 rows=1000000.00 loops=1)
              Index Cond: ((id >= 2500001) AND (id <= 3500000))
              Index Searches: 1
              Buffers: shared hit=2735
Planning Time: 0.060 ms
Execution Time: 271.020 ms
```

### Index Scan, fully cached

```
Index Scan using t_runs_id on t_runs  (cost=0.43..758322.37 rows=1003804 width=117) (actual time=0.013..74.404 rows=1000000.00 loops=1)
  Index Cond: ((id >= 2500001) AND (id <= 3500000))
  Index Searches: 1
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=25883
Planning Time: 0.045 ms
Execution Time: 85.451 ms
```

### Sort over Bitmap Heap Scan, fully cached

```
Sort  (cost=318241.92..320751.43 rows=1003804 width=117) (actual time=236.499..255.886 rows=1000000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=25395
  ->  Bitmap Heap Scan on t_runs  (cost=21301.43..218177.49 rows=1003804 width=117) (actual time=23.759..88.442 rows=1000000.00 loops=1)
        Recheck Cond: ((id >= 2500001) AND (id <= 3500000))
        Heap Blocks: exact=22660
        Prefetch: avg=1.00 max=1 capacity=1616
        Buffers: shared hit=25395
        ->  Bitmap Index Scan on t_runs_id  (cost=0.00..21050.48 rows=1003804 width=0) (actual time=21.036..21.036 rows=1000000.00 loops=1)
              Index Cond: ((id >= 2500001) AND (id <= 3500000))
              Index Searches: 1
              Buffers: shared hit=2735
Planning Time: 0.050 ms
Execution Time: 281.403 ms
```

### Index Scan, heap uncached, effective_io_concurrency=16

```
Index Scan using t_runs_id on t_runs  (cost=0.43..758322.37 rows=1003804 width=117) (actual time=0.173..102.452 rows=1000000.00 loops=1)
  Index Cond: ((id >= 2500001) AND (id <= 3500000))
  Index Searches: 1
  Prefetch: avg=74.08 max=82 capacity=272
  I/O: count=5104 waits=162 size=4.44 in-progress=15.95
  Buffers: shared hit=3223 read=22660
Planning Time: 0.048 ms
Execution Time: 114.561 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=16

```
Sort  (cost=318241.92..320751.43 rows=1003804 width=117) (actual time=233.686..252.854 rows=1000000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=2735 read=22660
  ->  Bitmap Heap Scan on t_runs  (cost=21301.43..218177.49 rows=1003804 width=117) (actual time=24.014..97.094 rows=1000000.00 loops=1)
        Recheck Cond: ((id >= 2500001) AND (id <= 3500000))
        Heap Blocks: exact=22660
        Prefetch: avg=80.64 max=109 capacity=272
        I/O: count=4609 waits=13 size=4.92 in-progress=15.95
        Buffers: shared hit=2735 read=22660
        ->  Bitmap Index Scan on t_runs_id  (cost=0.00..21050.48 rows=1003804 width=0) (actual time=21.112..21.113 rows=1000000.00 loops=1)
              Index Cond: ((id >= 2500001) AND (id <= 3500000))
              Index Searches: 1
              Buffers: shared hit=2735
Planning Time: 0.058 ms
Execution Time: 279.292 ms
```

### Index Scan, heap uncached, effective_io_concurrency=100

```
Index Scan using t_runs_id on t_runs  (cost=0.43..758322.37 rows=1003804 width=117) (actual time=0.120..96.125 rows=1000000.00 loops=1)
  Index Cond: ((id >= 2500001) AND (id <= 3500000))
  Index Searches: 1
  Prefetch: avg=124.67 max=127 capacity=1616
  I/O: count=5105 waits=7 size=4.44 in-progress=27.52
  Buffers: shared hit=3223 read=22660
Planning Time: 0.045 ms
Execution Time: 108.188 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Sort  (cost=318241.92..320751.43 rows=1003804 width=117) (actual time=234.735..253.888 rows=1000000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 157389kB
  Buffers: shared hit=2735 read=22660
  ->  Bitmap Heap Scan on t_runs  (cost=21301.43..218177.49 rows=1003804 width=117) (actual time=23.943..96.753 rows=1000000.00 loops=1)
        Recheck Cond: ((id >= 2500001) AND (id <= 3500000))
        Heap Blocks: exact=22660
        Prefetch: avg=251.01 max=256 capacity=1616
        I/O: count=4609 waits=8 size=4.92 in-progress=51.07
        Buffers: shared hit=2735 read=22660
        ->  Bitmap Index Scan on t_runs_id  (cost=0.00..21050.48 rows=1003804 width=0) (actual time=21.086..21.086 rows=1000000.00 loops=1)
              Index Cond: ((id >= 2500001) AND (id <= 3500000))
              Index Searches: 1
              Buffers: shared hit=2735
Planning Time: 0.061 ms
Execution Time: 278.989 ms
```

## t_sparse: one row per page, random order; 20k rows = 20k of 200k heap pages

```sql
SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id;
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Index Scan using t_sparse_id on t_sparse  (cost=0.42..76937.04 rows=20033 width=708) (actual time=0.010..23.139 rows=20000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 70000))
  Index Searches: 1
  Prefetch: avg=154.88 max=271 capacity=272
  I/O: count=2017 waits=73 size=1.00 in-progress=15.85
  Buffers: shared hit=18040 read=2018
Planning:
  Buffers: shared hit=11
Planning Time: 0.088 ms
Execution Time: 23.437 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Sort  (cost=60796.74..60846.83 rows=20033 width=708) (actual time=42.936..44.845 rows=20000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 14987kB
  Buffers: shared hit=18040 read=2018
  ->  Bitmap Heap Scan on t_sparse  (cost=429.76..59365.38 rows=20033 width=708) (actual time=3.834..40.898 rows=20000.00 loops=1)
        Recheck Cond: ((id >= 50001) AND (id <= 70000))
        Heap Blocks: exact=20000
        Prefetch: avg=160.04 max=271 capacity=272
        I/O: count=1992 waits=432 size=1.01 in-progress=15.89
        Buffers: shared hit=18040 read=2018
        ->  Bitmap Index Scan on t_sparse_id  (cost=0.00..424.75 rows=20033 width=0) (actual time=1.410..1.410 rows=20000.00 loops=1)
              Index Cond: ((id >= 50001) AND (id <= 70000))
              Index Searches: 1
              Buffers: shared hit=58
Planning Time: 0.059 ms
Execution Time: 45.068 ms
```

### Index Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Index Scan using t_sparse_id on t_sparse  (cost=0.42..76937.04 rows=20033 width=708) (actual time=0.012..22.874 rows=20000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 70000))
  Index Searches: 1
  Prefetch: avg=497.63 max=511 capacity=1616
  I/O: count=2017 waits=9 size=1.00 in-progress=51.38
  Buffers: shared hit=18040 read=2018
Planning Time: 0.059 ms
Execution Time: 23.188 ms
```

### Sort over Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Sort  (cost=60796.74..60846.83 rows=20033 width=708) (actual time=31.117..32.682 rows=20000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 14987kB
  Buffers: shared hit=18040 read=2018
  ->  Bitmap Heap Scan on t_sparse  (cost=429.76..59365.38 rows=20033 width=708) (actual time=3.773..29.058 rows=20000.00 loops=1)
        Recheck Cond: ((id >= 50001) AND (id <= 70000))
        Heap Blocks: exact=20000
        Prefetch: avg=501.17 max=511 capacity=1616
        I/O: count=1992 waits=13 size=1.01 in-progress=50.90
        Buffers: shared hit=18040 read=2018
        ->  Bitmap Index Scan on t_sparse_id  (cost=0.00..424.75 rows=20033 width=0) (actual time=1.362..1.362 rows=20000.00 loops=1)
              Index Cond: ((id >= 50001) AND (id <= 70000))
              Index Searches: 1
              Buffers: shared hit=58
Planning Time: 0.063 ms
Execution Time: 32.916 ms
```

### Index Scan, fully cached

```
Index Scan using t_sparse_id on t_sparse  (cost=0.42..77435.23 rows=20170 width=708) (actual time=0.007..7.121 rows=20000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 70000))
  Index Searches: 1
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=20058
Planning Time: 0.022 ms
Execution Time: 7.349 ms
```

### Sort over Bitmap Heap Scan, fully cached

```
Sort  (cost=61134.36..61184.79 rows=20170 width=708) (actual time=16.264..17.412 rows=20000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 14987kB
  Buffers: shared hit=20058
  ->  Bitmap Heap Scan on t_sparse  (cost=431.16..59692.22 rows=20170 width=708) (actual time=3.666..14.568 rows=20000.00 loops=1)
        Recheck Cond: ((id >= 50001) AND (id <= 70000))
        Heap Blocks: exact=20000
        Prefetch: avg=1.00 max=1 capacity=1616
        Buffers: shared hit=20058
        ->  Bitmap Index Scan on t_sparse_id  (cost=0.00..426.12 rows=20170 width=0) (actual time=1.279..1.280 rows=20000.00 loops=1)
              Index Cond: ((id >= 50001) AND (id <= 70000))
              Index Searches: 1
              Buffers: shared hit=58
Planning Time: 0.046 ms
Execution Time: 17.639 ms
```

### Index Scan, heap uncached, effective_io_concurrency=16

```
Index Scan using t_sparse_id on t_sparse  (cost=0.42..77435.23 rows=20170 width=708) (actual time=0.164..287.923 rows=20000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 70000))
  Index Searches: 1
  Prefetch: avg=15.99 max=16 capacity=272
  I/O: count=19997 waits=4920 size=1.00 in-progress=15.99
  Buffers: shared hit=58 read=20000
Planning Time: 0.055 ms
Execution Time: 288.533 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=16

```
Sort  (cost=61134.36..61184.79 rows=20170 width=708) (actual time=241.799..243.996 rows=20000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 14987kB
  Buffers: shared hit=58 read=20000
  ->  Bitmap Heap Scan on t_sparse  (cost=431.16..59692.22 rows=20170 width=708) (actual time=4.011..235.514 rows=20000.00 loops=1)
        Recheck Cond: ((id >= 50001) AND (id <= 70000))
        Heap Blocks: exact=20000
        Prefetch: avg=17.87 max=25 capacity=272
        I/O: count=18007 waits=4646 size=1.11 in-progress=15.99
        Buffers: shared hit=58 read=20000
        ->  Bitmap Index Scan on t_sparse_id  (cost=0.00..426.12 rows=20170 width=0) (actual time=1.411..1.411 rows=20000.00 loops=1)
              Index Cond: ((id >= 50001) AND (id <= 70000))
              Index Searches: 1
              Buffers: shared hit=58
Planning Time: 0.058 ms
Execution Time: 244.226 ms
```

### Index Scan, heap uncached, effective_io_concurrency=100

```
Index Scan using t_sparse_id on t_sparse  (cost=0.42..77435.23 rows=20170 width=708) (actual time=0.165..127.933 rows=20000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 70000))
  Index Searches: 1
  Prefetch: avg=99.66 max=100 capacity=1616
  I/O: count=19997 waits=813 size=1.00 in-progress=99.66
  Buffers: shared hit=58 read=20000
Planning Time: 0.046 ms
Execution Time: 128.512 ms
```

### Sort over Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Sort  (cost=61134.36..61184.79 rows=20170 width=708) (actual time=100.118..102.336 rows=20000.00 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 14987kB
  Buffers: shared hit=58 read=20000
  ->  Bitmap Heap Scan on t_sparse  (cost=431.16..59692.22 rows=20170 width=708) (actual time=3.952..92.387 rows=20000.00 loops=1)
        Recheck Cond: ((id >= 50001) AND (id <= 70000))
        Heap Blocks: exact=20000
        Prefetch: avg=110.78 max=123 capacity=1616
        I/O: count=18007 waits=382 size=1.11 in-progress=99.63
        Buffers: shared hit=58 read=20000
        ->  Bitmap Index Scan on t_sparse_id  (cost=0.00..426.12 rows=20170 width=0) (actual time=1.413..1.414 rows=20000.00 loops=1)
              Index Cond: ((id >= 50001) AND (id <= 70000))
              Index Searches: 1
              Buffers: shared hit=58
Planning Time: 0.058 ms
Execution Time: 102.563 ms
```

## Index-only scan with partial visibility-map coverage

Setup: `illustrate_setup_ios.sql` (t_sparse_ios: one primary and one filler row per page, random page order, unlogged).  Coverage is reduced by deleting the filler row on a pseudo-random set of pages and running `VACUUM (INDEX_CLEANUP off, ANALYZE)` + `REINDEX`, which leaves an LP_DEAD stub on those pages: no later VACUUM or scan can set them all-visible, so the index-only scan has to fetch them (the --ios suite's recipe).  The scan touches 40% of the pages; at 40% coverage the stock planner gives up on the index-only scan (costs in the outputs below), while the cost model change keeps it.

```sql
SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 130000;
```

| all-visible pages | plan | 90% cached, eic=16 | 90% cached, eic=100 | fully cached | heap uncached, eic=16 | heap uncached, eic=100 | stock planner picks | with the cost change |
|---|---|---|---|---|---|---|---|---|
| 100% | Index Only Scan | - | - | 8.2 | 8.2 | 8.2 | Index Only Scan | Index Only Scan |
| 100% | Bitmap Heap Scan | - | - | 47.2 | 510.3 | 256.6 |  | |
| 60% | Index Only Scan | - | - | 19.9 | 253.1 | 126.5 | Index Only Scan | Index Only Scan |
| 60% | Bitmap Heap Scan | - | - | 58.8 | 514.2 | 255.8 |  | |
| 40% | Index Only Scan | 58.5 | 60.9 | 23.6 | 382.6 | 188.0 | Bitmap Heap Scan | Index Only Scan |
| 40% | Bitmap Heap Scan | 99.1 | 94.6 | 57.8 | 507.1 | 256.0 |  | |

### 40% of pages all-visible

Planner costs (stock at eic 16 and 100, then with the cost change at eic=100):

```
-- stock_eic16
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0)
        Index Cond: ((id >= 50001) AND (id <= 130000))

-- stock_eic100
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0)
        Index Cond: ((id >= 50001) AND (id <= 130000))

-- patch_eic100
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..60794.85 rows=78544 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))
```

#### Index Only Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..160331.06 rows=78544 width=4) (actual time=0.015..57.327 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 48093
  Index Searches: 1
  Prefetch: avg=153.55 max=271 capacity=272
  I/O: count=4813 waits=15 size=1.00 in-progress=15.58
  Buffers: shared hit=110717 read=4814
Planning Time: 0.056 ms
Execution Time: 58.471 ms
```

#### Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=16

```
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4) (actual time=16.835..97.882 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=165.91 max=271 capacity=272
  I/O: count=7667 waits=533 size=1.04 in-progress=15.96
  Buffers: shared hit=72224 read=7997
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0) (actual time=5.940..5.941 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.052 ms
Execution Time: 99.143 ms
```

#### Index Only Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..160331.06 rows=78544 width=4) (actual time=0.013..59.837 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 48093
  Index Searches: 1
  Prefetch: avg=412.62 max=511 capacity=1616
  I/O: count=4813 waits=9 size=1.00 in-progress=42.18
  Buffers: shared hit=110717 read=4814
Planning Time: 0.054 ms
Execution Time: 60.938 ms
```

#### Bitmap Heap Scan, 90% of heap pages cached, effective_io_concurrency=100

```
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4) (actual time=17.706..93.308 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=507.36 max=511 capacity=1616
  I/O: count=7667 waits=9 size=1.04 in-progress=49.58
  Buffers: shared hit=72224 read=7997
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0) (actual time=6.783..6.783 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.056 ms
Execution Time: 94.570 ms
```

#### Index Only Scan, fully cached

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..160331.06 rows=78544 width=4) (actual time=0.008..22.777 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 48093
  Index Searches: 1
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=115531
Planning Time: 0.025 ms
Execution Time: 23.647 ms
```

#### Bitmap Heap Scan, fully cached

```
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4) (actual time=17.423..56.839 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=80221
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0) (actual time=6.456..6.457 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.046 ms
Execution Time: 57.759 ms
```

#### Index Only Scan, heap uncached, effective_io_concurrency=16

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..160331.06 rows=78544 width=4) (actual time=0.012..380.943 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 48093
  Index Searches: 1
  Prefetch: avg=16.00 max=16 capacity=272
  I/O: count=48090 waits=10315 size=1.00 in-progress=16.00
  Buffers: shared hit=67438 read=48093
Planning Time: 0.051 ms
Execution Time: 382.621 ms
```

#### Bitmap Heap Scan, heap uncached, effective_io_concurrency=16

```
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4) (actual time=18.271..505.117 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=27.31 max=51 capacity=272
  I/O: count=48040 waits=10815 size=1.67 in-progress=16.00
  Buffers: shared hit=222 read=79999
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0) (actual time=7.248..7.249 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.051 ms
Execution Time: 507.076 ms
```

#### Index Only Scan, heap uncached, effective_io_concurrency=100

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..160331.06 rows=78544 width=4) (actual time=0.011..186.114 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 48093
  Index Searches: 1
  Prefetch: avg=99.84 max=100 capacity=1616
  I/O: count=48090 waits=215 size=1.00 in-progress=99.84
  Buffers: shared hit=67438 read=48093
Planning Time: 0.049 ms
Execution Time: 187.977 ms
```

#### Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Bitmap Heap Scan on t_sparse_ios  (cost=1669.50..152613.15 rows=78544 width=4) (actual time=18.159..253.794 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=166.95 max=209 capacity=1616
  I/O: count=48040 waits=242 size=1.67 in-progress=99.86
  Buffers: shared hit=222 read=79999
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1649.86 rows=78544 width=0) (actual time=7.156..7.156 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.050 ms
Execution Time: 256.022 ms
```

### 100% of pages all-visible

```
-- stock_eic16
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..2512.50 rows=81004 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))

-- stock_eic100
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..2512.50 rows=81004 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))

-- patch_eic100
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..2512.50 rows=81004 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))
```

#### Index Only Scan, fully cached

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..2512.50 rows=81004 width=4) (actual time=0.008..7.270 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 0
  Index Searches: 1
  Buffers: shared hit=67438
Planning Time: 0.025 ms
Execution Time: 8.150 ms
```

#### Bitmap Heap Scan, fully cached

```
Bitmap Heap Scan on t_sparse_ios  (cost=1722.71..155105.88 rows=81004 width=4) (actual time=16.878..46.317 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=80221
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1702.46 rows=81004 width=0) (actual time=5.973..5.973 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.037 ms
Execution Time: 47.207 ms
```

#### Index Only Scan, heap uncached, effective_io_concurrency=100

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..2512.50 rows=81004 width=4) (actual time=0.011..7.334 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 0
  Index Searches: 1
  Buffers: shared hit=67438
Planning Time: 0.027 ms
Execution Time: 8.205 ms
```

#### Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Bitmap Heap Scan on t_sparse_ios  (cost=1722.71..155105.88 rows=81004 width=4) (actual time=17.648..254.493 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=166.96 max=209 capacity=1616
  I/O: count=48040 waits=283 size=1.67 in-progress=99.86
  Buffers: shared hit=222 read=79999
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1702.46 rows=81004 width=0) (actual time=6.572..6.573 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.050 ms
Execution Time: 256.567 ms
```

### 60% of pages all-visible

```
-- stock_eic16
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..110189.85 rows=80683 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))

-- stock_eic100
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..110189.85 rows=80683 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))

-- patch_eic100
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..42284.67 rows=80683 width=4)
  Index Cond: ((id >= 50001) AND (id <= 130000))
```

#### Index Only Scan, fully cached

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..110189.85 rows=80683 width=4) (actual time=0.011..18.994 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 32010
  Index Searches: 1
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=99448
Planning Time: 0.034 ms
Execution Time: 19.873 ms
```

#### Bitmap Heap Scan, fully cached

```
Bitmap Heap Scan on t_sparse_ios  (cost=1715.42..154785.07 rows=80683 width=4) (actual time=17.165..57.869 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=1.00 max=1 capacity=1616
  Buffers: shared hit=80221
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1695.25 rows=80683 width=0) (actual time=6.272..6.273 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.048 ms
Execution Time: 58.791 ms
```

#### Index Only Scan, heap uncached, effective_io_concurrency=100

```
Index Only Scan using t_sparse_ios_id on t_sparse_ios  (cost=0.42..110189.85 rows=80683 width=4) (actual time=0.011..124.727 rows=80000.00 loops=1)
  Index Cond: ((id >= 50001) AND (id <= 130000))
  Heap Fetches: 32010
  Index Searches: 1
  Prefetch: avg=99.77 max=100 capacity=1616
  I/O: count=32007 waits=76 size=1.00 in-progress=99.77
  Buffers: shared hit=67438 read=32010
Planning Time: 0.039 ms
Execution Time: 126.499 ms
```

#### Bitmap Heap Scan, heap uncached, effective_io_concurrency=100

```
Bitmap Heap Scan on t_sparse_ios  (cost=1715.42..154785.07 rows=80683 width=4) (actual time=18.171..253.623 rows=80000.00 loops=1)
  Recheck Cond: ((id >= 50001) AND (id <= 130000))
  Heap Blocks: exact=79999
  Prefetch: avg=166.97 max=209 capacity=1616
  I/O: count=48040 waits=246 size=1.67 in-progress=99.87
  Buffers: shared hit=222 read=79999
  ->  Bitmap Index Scan on t_sparse_ios_id  (cost=0.00..1695.25 rows=80683 width=0) (actual time=7.165..7.166 rows=80000.00 loops=1)
        Index Cond: ((id >= 50001) AND (id <= 130000))
        Index Searches: 1
        Buffers: shared hit=222
Planning Time: 0.049 ms
Execution Time: 255.849 ms
```
