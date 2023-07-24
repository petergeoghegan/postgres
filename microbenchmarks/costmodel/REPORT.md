# Plain index scan costing under prefetching: measurements and a cost model

Rig: `claude_work/costmodel/` (rig.py, analyze.py, make_tables.py,
gen_tables*.sql, models*.json, post_grid.sh, delay_phase.sh).  Scratch
release build of the index-prefetch branch with two developer GUCs
(worktree `costmodel-exp`: `debug_index_cost_model` selects a candidate
formula, `debug_disable_index` hides an index so each alternative path can
be priced exactly); the committed form is the "Account for read stream
latency hiding" commit on the branch (three regression expected outputs
change, see section 8).  Clusters:
own NVMe cluster (16 GB shared_buffers, io_uring, io_combine_limit=16),
and the user's dm-delay cluster (1 ms read latency, 4 GB shared_buffers)
for the network-storage case.

Section 7 holds the dm-delay results, section 8 the recommendation and
the limitations.

## 1. What was measured

Two tables, identical heap order (by `id`), one index per layout:

| table | rows | heap pages | rows/page |
|---|---|---|---|
| td ("dense") | 10,000,000 | 204,082 | 49 |
| ts ("sparse") | 200,000 | 200,000 | 1 |

(dm-delay: half size, 5,000,000 / 100,000 rows.)

Layouts (index order relative to heap order):

| layout | meaning | pg_stats correlation |
|---|---|---|
| corr | index order == heap order | 1.0 |
| rand | random permutation | ~0 |
| run4 | 4-page runs in random order (local adjacency) | ~0 |
| jit | heap order jittered by +-16 pages | 0.99999994 |
| dup64 (td) | 64 duplicate keys per group of 64 adjacent rows (~1.3 pages), groups in hash order; the `t_randomized` shape of the slides | ~0 |
| run16 (ts) | 16-page runs in random order | ~0 |

All keys are dense permutations, so `BETWEEN lo AND lo+k-1` returns exactly
k rows for every layout and the planner's selectivity estimates are the
same across layouts.  Selectivity: 9 steps from 0.01% to 50% of the rows.

Plans measured per cell (forced with enable_* GUCs; EXPLAIN (ANALYZE,
TIMING OFF, BUFFERS, IO)): plain index scan, bitmap heap scan, bitmap heap
scan + Sort, seq scan, seq scan + Sort.  Cache states: cold (heap evicted
from shared_buffers with pg_buffercache_evict_relation and from the page
cache with posix_fadvise(DONTNEED), verified with mincore; index and VM
warm) at effective_io_concurrency 16 (the shipped default) and 100 (the
slides' setting); warm (everything in shared_buffers).  An O_DIRECT
heartbeat keeps the NVMe out of its ~8 ms power-nap state during cold runs
(without it every cold cell below ~1000 pages measured 8.4 ms).  The rig
tables are loaded as UNLOGGED: under synchronous_commit=off a logged table's
VACUUM FREEZE right after the load cannot set XMIN_COMMITTED hint bits and
leaves most all-visible bits unset (a known issue), which the plain index
scans here do not notice but an index-only scan does.

Also measured: two-index cells (`a_corr BETWEEN wide AND b_rand BETWEEN
narrow`, i.e. a sequential-but-unselective index against a
random-but-selective one, 5x5 grid of widths, plus BitmapAnd and seq scan),
and LIMIT cells (`ORDER BY a_corr LIMIT 100` with a filter that is either
uniformly spread or concentrated in the last 10% of the heap, the blog
posts' shape).

Scoring: for every cell the planner's choice among the measured plans is
taken from the EXPLAIN cost of each forced plan under a given model, and
regret = time(chosen plan) / time(fastest measured plan).  "mix90" scores
the choice against 0.9 x warm + 0.1 x cold time, the cache-hit assumption
the documentation gives for random_page_cost.  In the tables "+a/-b" is the
number of cells a model makes better / worse than the stock model by more
than 1.5x.

## 2. Physics (NVMe, cold)

Per-page cost of the heap access, (cold - warm) / pages read, td table
(ts behaves the same; its file sits in a faster region of the SSD, ~1.8x
lower latency):

| layout | density of pages touched | plain us/page | bitmap us/page | bitmap combining (pages/I/O) |
|---|---|---|---|---|
| rand, eic=16 | 0.5% .. 99% | 12.2 .. 13.7 (flat) | 16.5, 11.3, 9.1, 6.9, 4.0, 1.4, 0.9 | 1.0, 1.05, 1.27, 1.63, 2.69, 9.2, 15.3 |
| rand, eic=100 | 22% / 39% / 63% | 6.4 / 6.5 / 6.5 | 4.4 / 3.4 / 2.6 | same |
| run4, dup64, run16 | any | ~= bitmap | | both 2-16 |
| corr | any | ~= bitmap ~= seq | | both 16 |
| seq scan (ts, I/O bound) | 100% | 1.4 | | 16 |

1. A plain scan in random order gets no benefit from density: it issues
   one I/O per heap page regardless, because combining needs adjacent block
   numbers in request order.  Its per-page cost is latency / concurrency:
   14 us at eic=16, 6.5 us at eic=100.  (The device delivers ~2.2x more
   IOPS at the deeper queue, not 6x; the backend's per-I/O CPU and the
   device's IOPS ceiling cap the gain.)
2. A bitmap heap scan's per-page cost falls with density exactly as its
   combining factor rises, 1/(1-density) up to io_combine_limit.  The stock
   `sqrt(pages/T)` interpolation tracks the measured bitmap/plain ratio well
   up to ~40% density (model 1.88 vs measured 1.85 at 39%; 2.5 vs 2.5-2.9 at
   63%) and is pessimistic for the bitmap above that.
3. So the stock planner's relative I/O costing of plain vs bitmap is
   accurate for truly random order at both eic settings.  Giving plain scans
   the bitmap's density discount ("interpolate like the bitmap") under-costs
   them by up to 3x at 60% density and more above: too far (last row of the
   tables below).
4. With local adjacency (run4, dup64, run16, corr) the plain scan combines
   as well as the bitmap scan and the two cost the same per page; the
   planner cannot see this (correlation ~0 for run4/dup64/run16).  dup64 is
   the slides' RB3 shape: `t_randomized` stores each key's 32 duplicates on
   adjacent pages, so the plain scan combined 2.4 pages per I/O against the
   bitmap's 2.8 and the two were at parity cold.
5. Relative to a sequential page, a prefetched random page costs 5-10x at
   eic=16 and 2.5-5x at eic=100 on this NVMe (13x and 4.7x on the 1 ms
   device).  random_page_cost=4 is therefore about right for cold prefetched
   reads at the default eic, and too high by about 2x at eic=100.  Before
   the patch the same page cost 130-225 us (synchronous), 50-170x
   sequential: the stock model was optimistic by an order of magnitude for
   cold plain scans, and only the bitmap's density discount kept the
   planner away from them.  The slides' "planner bias in favor of bitmap
   scans" is, cold and at the default eic, not a bias of cost_index against
   cost_bitmap_heap_scan; it is the cache-state bet (section 4) plus the
   planner's blindness to adjacency.

## 3. The remaining bitmap advantage ("pinning")

Warm (no I/O), td rand, per-row CPU: plain = 0.41 us/row, flat in k (one
buffer lookup/pin/unpin per TID); bitmap = 0.092 us/row + 0.78 us/page.

* With k matching rows per page in random order the bitmap's heap-side CPU
  is lower than the plain scan's once k > ~2.5; at k=25 the plain scan is
  3.4x slower warm (2081 vs 619 ms for 5M rows).
* With an ORDER BY the Sort costs 0.3-0.8 us/row, which moves the warm
  break-even to k ~ 14.
* Cold, the bitmap wins earlier, because the same k that puts several TIDs
  on each page also raises density and hence combining: 1.4x at 22%
  density, 1.9x at 39%, 2.5-2.9x at 63%, 5-8x at 92%+ (NVMe); 6-10x at 92%+
  on the 1 ms device.
* At k=1 (sparse table) the plain scan is 2x cheaper per row warm and equal
  cold at low density.
* "Small amounts of I/O combining" on the plain side (run4, dup64) close the
  gap completely, cold and warm.

A per-TID CPU charge for plain scans (cpu_tuple_cost per extra block switch,
and 3x that) was tried as a model ingredient and changed no decision of
note: I/O terms dominate wherever it would matter.

## 4. Stock model regret, NVMe

Single-index cells (180 per state):

| state | geomean | max | cells > 1.5x |
|---|---|---|---|
| cold, eic=16 | 1.22 | 4.2 | 36/180 |
| cold, eic=100 | 1.25 | 4.7 | 41/180 |
| warm | 1.73 | 7.2 | 89/180 |
| mix90 (eic=16 cold) | 1.37 | 5.8 | 45/180 |

Per layout (geomean, cells > 1.5x):

| layout | cold16 | cold100 | warm | mix90@16 |
|---|---|---|---|---|
| corr | 1.02 (0) | 1.03 (0) | 1.00 (0) | 1.01 (0) |
| rand | 1.05 (2) | 1.01 (0) | 2.01 (28) | 1.10 (0) |
| run4 | 1.25 (6) | 1.33 (8) | 2.23 (27) | 1.64 (16) |
| jit | 1.67 (22) | 1.60 (24) | 1.33 (9) | 1.35 (9) |
| dup64 | 1.30 (6) | 1.69 (8) | 2.68 (10) | 2.24 (9) |
| run16 | 1.11 (0) | 1.15 (1) | 2.45 (15) | 1.73 (11) |

Mistake families:

* rand: right cold; warm it picks bitmap+Sort where the plain scan wins
  2-7x, but at a 90% hit rate the choice is still right (1.10).
* run4 / dup64 / run16 (local adjacency): wrong in every state, 1.3-2.7x,
  because the plain scan is priced as random while it combines like the
  bitmap.  The slides' case.
* jit (correlation 0.99999994, the largest float4 below 1): picks plain
  where bitmap wins 1.6-6.5x.  csquared ~ 1 makes the planner assume a
  sequential pass; the plain scan re-pins per row and re-requests pages
  inside the jitter window (I/O count 30-130% above distinct pages).  A
  bias *for* plain scans that no page-cost model fixes; it would need a
  statistic pg_stats does not have.

## 5. Candidate models (NVMe)

Candidates tried (all applied only when the scan reads >= 2 heap pages
and, for the plain scan, loop_count <= 1; "both scans" = cost_index and
cost_bitmap_heap_scan):

* stock with random_page_cost 1.1 (what the blog posts do) and with the
  whole I/O side scaled down (seq_page_cost 0.1, random_page_cost 0.4).
* A: scale the random-access penalty (rpc - spc) of prefetched heap pages
  by default_eic / eic (no-op at or below the default 16), both scans, with
  the first 4 pages and one page per prefetch-distance doubling kept at
  full penalty.  The first version of the patch.
* A-whole: as A but scaling the whole per-page cost including spc.
  Rejected: at eic=50 (select_parallel.sql) it makes a bitmap heap scan of
  98% of a table cheaper than a seq scan of the same pages, which fails the
  regression suite and is physically wrong (the seq scan streams too).
* B: as A with 1/eic instead of 16/eic, i.e. Little's law against the
  synchronous latency: a discount at the default eic too ("simulate a low
  random_page_cost for the scans that can prefetch").  The committed patch
  is B plus the page-estimate floor of section 8.
* plain-only variants of B, and the plain scan given the bitmap's sqrt(P/T)
  density discount (model 1 of the GUC); density-faded and per-TID-pin
  variants (not shown; they add nothing).

Single-index cells, all layouts:

| model | cold16@16 | cold100@100 | warm@16 | mix90_16@16 | mix90_100@100 |
|---|---|---|---|---|---|
| stock (rpc=4) | 1.22 (max 4.2, 36/180 >1.5x) | 1.25 (max 4.7, 41/180 >1.5x) | 1.73 (max 7.2, 89/180 >1.5x) | 1.37 (max 5.8, 45/180 >1.5x) | 1.43 (max 6.2, 58/180 >1.5x) |
| stock, rpc=1.1 | 1.21 (max 7.3, 30/180; +15/-9) | 1.18 (max 5.8, 30/180; +19/-8) | 1.23 (max 5.5, 39/180; +57/-7) | 1.13 (max 4.0, 17/180; +33/-5) | 1.14 (max 3.9, 24/180; +39/-5) |
| stock, spc=0.1 rpc=0.4 | 1.19 (max 4.2, 32/180; +7/-3) | 1.20 (max 3.4, 35/180; +9/-3) | 1.39 (max 7.2, 57/180; +32/-0) | 1.21 (max 5.2, 25/180; +20/-0) | 1.24 (max 6.2, 35/180; +23/-0) |
| A: penalty x 16/eic, both scans (first version) | 1.22 (max 4.2, 36/180; +0/-0) | 1.22 (max 3.5, 37/180; +7/-3) | 1.73 (max 7.2, 89/180; +0/-0) | 1.37 (max 5.8, 45/180; +0/-0) | 1.30 (max 6.2, 44/180; +15/-1) |
| B: penalty x 1/eic, both scans | 1.19 (max 4.5, 29/180; +12/-4) | 1.16 (max 4.1, 29/180; +20/-8) | 1.29 (max 7.2, 47/180; +46/-4) | 1.16 (max 3.7, 20/180; +28/-3) | 1.12 (max 3.5, 20/180; +44/-6) |
| penalty x 1/eic, plain only | 1.23 (max 4.5, 35/180; +12/-12) | 1.20 (max 4.1, 36/180; +16/-11) | 1.07 (max 3.2, 11/180; +78/-0) | 1.08 (max 3.1, 11/180; +35/-0) | 1.07 (max 3.1, 11/180; +48/-1) |
| plain gets the bitmap's sqrt(P/T) discount | 1.28 (max 6.4, 39/180; +12/-16) | 1.21 (max 4.1, 38/180; +16/-13) | 1.07 (max 3.2, 12/180; +79/-2) | 1.10 (max 3.1, 14/180; +35/-3) | 1.08 (max 3.1, 12/180; +48/-2) |

Layout rand only (truly random order; this is where discounts hurt):

| model | cold16@16 | cold100@100 | warm@16 | mix90_16@16 | mix90_100@100 |
|---|---|---|---|---|---|
| stock (rpc=4) | 1.05 (max 1.7, 2/36) | 1.01 (max 1.1, 0/36) | 2.01 (max 6.6, 28/36) | 1.10 (max 1.5, 0/36) | 1.21 (max 2.0, 6/36) |
| stock, rpc=1.1 | 1.40 (max 7.3, 10/36; +0/-8) | 1.25 (max 5.8, 8/36; +0/-8) | 1.31 (max 3.6, 13/36; +16/-1) | 1.09 (max 4.0, 1/36; +0/-1) | 1.09 (max 3.9, 1/36; +6/-1) |
| A (first version) | 1.05 (max 1.7, 2/36; +0/-0) | 1.07 (max 2.3, 3/36; +0/-3) | 2.01 (max 6.6, 28/36; +0/-0) | 1.10 (max 1.5, 0/36; +0/-0) | 1.15 (max 2.0, 4/36; +2/-0) |
| B | 1.24 (max 4.5, 7/36; +0/-4) | 1.23 (max 4.1, 8/36; +0/-8) | 1.43 (max 6.6, 16/36; +12/-0) | 1.06 (max 1.5, 0/36; +0/-0) | 1.05 (max 2.4, 1/36; +6/-1) |
| penalty x 1/eic, plain only | 1.46 (max 4.5, 12/36; +0/-11) | 1.36 (max 4.1, 11/36; +0/-11) | 1.00 (max 1.0, 0/36; +28/-0) | 1.06 (max 2.1, 1/36; +0/-0) | 1.03 (max 2.4, 1/36; +6/-1) |
| plain gets the bitmap's sqrt(P/T) discount | 1.70 (max 6.4, 15/36; +0/-14) | 1.45 (max 4.1, 13/36; +0/-13) | 1.04 (max 2.1, 2/36; +28/-2) | 1.14 (max 2.7, 4/36; +0/-3) | 1.06 (max 2.4, 2/36; +6/-2) |

Clustered layouts run4 + dup64 + run16 (this is where the stock model is
biased against plain scans):

| model | cold16@16 | cold100@100 | warm@16 | mix90_16@16 | mix90_100@100 |
|---|---|---|---|---|---|
| stock (rpc=4) | 1.22 (max 4.2, 12/72) | 1.36 (max 4.7, 17/72) | 2.39 (max 7.2, 52/72) | 1.79 (max 5.8, 36/72) | 1.91 (max 6.2, 43/72) |
| stock, rpc=1.1 | 1.06 (max 2.1, 1/72; +12/-1) | 1.06 (max 1.6, 1/72; +16/-0) | 1.16 (max 3.1, 11/72; +41/-0) | 1.07 (max 1.7, 3/72; +33/-0) | 1.10 (max 1.8, 10/72; +33/-0) |
| A (first version) | 1.22 (max 4.2, 12/72; +0/-0) | 1.23 (max 3.5, 11/72; +6/-0) | 2.39 (max 7.2, 52/72; +0/-0) | 1.79 (max 5.8, 36/72; +0/-0) | 1.54 (max 6.2, 30/72; +13/-0) |
| B | 1.08 (max 2.4, 2/72; +10/-0) | 1.05 (max 1.6, 1/72; +16/-0) | 1.28 (max 7.2, 18/72; +34/-0) | 1.15 (max 3.7, 8/72; +28/-0) | 1.05 (max 1.8, 5/72; +38/-0) |
| penalty x 1/eic, plain only | 1.07 (max 2.1, 1/72; +12/-1) | 1.04 (max 1.6, 1/72; +16/-0) | 1.02 (max 1.7, 2/72; +50/-0) | 1.02 (max 1.7, 1/72; +35/-0) | 1.01 (max 1.7, 1/72; +42/-0) |

Where the cold losses of A and B are: without exception the random-order
cells at 39-100% density with an ORDER BY (plain chosen instead of
bitmap+Sort or seq+Sort), 1.6-4.5x slower cold on NVMe, faster warm.  For
A there are three such cells, all at eic=100 and 92-100% density.

## 6. Two-index and LIMIT cells (NVMe)

Two-index (sequential-but-unselective index vs random-but-selective index,
50 cells):

| model | cold16@16 | cold100@100 | warm@16 | mix90_16@16 | mix90_100@100 |
|---|---|---|---|---|---|
| stock (rpc=4) | 1.01 (max 1.3, 0/50) | 1.03 (max 1.7, 1/50) | 2.24 (max 22.9, 27/50) | 1.20 (max 4.7, 9/50) | 1.32 (max 6.2, 11/50) |
| stock, rpc=1.1 | 1.04 (max 1.9, 1/50; +0/-1) | 1.02 (max 1.5, 1/50; +1/-0) | 2.19 (max 22.9, 26/50; +2/-1) | 1.18 (max 4.7, 8/50; +2/-1) | 1.30 (max 6.2, 10/50; +2/-1) |
| A (first version) | 1.01 (max 1.3, 0/50; +0/-0) | 1.01 (max 1.5, 0/50; +1/-0) | 2.24 (max 22.9, 27/50; +0/-0) | 1.20 (max 4.7, 9/50; +0/-0) | 1.28 (max 6.2, 9/50; +2/-0) |
| B | 1.02 (max 1.3, 0/50; +0/-0) | 1.02 (max 1.5, 1/50; +1/-0) | 2.16 (max 22.9, 25/50; +2/-0) | 1.17 (max 4.7, 7/50; +2/-0) | 1.30 (max 6.2, 10/50; +2/-1) |
| penalty x 1/eic, plain only | 1.30 (max 7.7, 9/50; +0/-9) | 1.31 (max 5.6, 11/50; +0/-11) | 1.36 (max 11.1, 11/50; +16/-0) | 1.10 (max 2.3, 4/50; +6/-1) | 1.07 (max 2.3, 3/50; +7/-0) |

The stock model is right cold and wrong warm (it picks the sequential
index or a BitmapAnd where the selective index is 2-23x faster warm), and
no I/O-side model changes that, rpc=1.1 included.  The reason is on the
CPU side: the wide correlated scan's cost is `rows x cpu_tuple_cost`, and
on this hardware a row costs the plain scan 0.4 us against a prefetched
page's 6-14 us, while the planner prices a row at 1/80 of a sequential
page.  Lowering random_page_cost helps only when the selective scan is tiny
(the blog posts' 500 pages), which is the point the user made: those cases
are not about random_page_cost vs seq_page_cost.

LIMIT cells: with uniformly spread matches the stock planner is right
(pkey scan + LIMIT stops early).  With matches concentrated in the last 10%
of the heap it picks the pkey scan at every width >= 0.1% and reads 90% of
the table, 100-1500x slower than the selective index; geomean regret
5.9-12.4 for every model.  rpc=1.1 and B flip two of the sixteen cells, the
others stay.  This is the LIMIT/uniformity assumption, outside the page-cost
model.

## 7. dm-delay (1 ms read latency)

Same grid on the user's dm-delay cluster (half-size tables: 5M / 100k rows,
4 GB shared_buffers, io_uring).  Physics: a cold random plain scan costs
62 us/page at eic=16 and 17 us/page at eic=100 (1 ms / 16, 1 ms / 100 plus
submission overhead); a seq scan 3.6-4.8 us/page; so random prefetched =
13x sequential at eic=16 and 4.7x at eic=100, the same ratios as NVMe
within a factor of 1.5.  At eic=16 the stock random_page_cost=4 is
optimistic for this device (stock regret on random-order cells cold16 =
1.47, 12/36 cells, against 1.05 on NVMe): it picks plain scans that are
2-4x slower than the bitmap alternative.

Single-index cells, all layouts:

| model | cold16@16 | cold100@100 | warm@16 | mix90_16@16 | mix90_100@100 |
|---|---|---|---|---|---|
| stock (rpc=4) | 1.41 (max 5.9, 52/180) | 1.31 (max 5.4, 44/180) | 1.70 (max 6.6, 85/180) | 1.34 (max 3.9, 52/180) | 1.35 (max 5.5, 47/180) |
| stock, rpc=1.1 | 1.57 (max 17.2, 61/180; +3/-15) | 1.34 (max 6.5, 46/180; +13/-15) | 1.21 (max 3.7, 35/180; +57/-7) | 1.28 (max 5.4, 41/180; +21/-11) | 1.19 (max 3.7, 28/180; +26/-7) |
| A (first version) | 1.41 (max 5.9, 52/180; +0/-0) | 1.30 (max 4.9, 42/180; +7/-5) | 1.70 (max 6.6, 85/180; +0/-0) | 1.34 (max 3.9, 52/180; +0/-0) | 1.28 (max 5.0, 42/180; +8/-3) |
| B | 1.49 (max 12.9, 56/180; +2/-6) | 1.31 (max 5.8, 43/180; +14/-13) | 1.28 (max 4.7, 45/180; +44/-4) | 1.26 (max 3.7, 40/180; +18/-6) | 1.16 (max 2.8, 24/180; +29/-6) |
| penalty x 1/eic, plain only | 1.61 (max 12.9, 62/180; +1/-17) | 1.42 (max 5.8, 52/180; +10/-19) | 1.06 (max 2.8, 10/180; +75/-0) | 1.29 (max 5.7, 45/180; +19/-13) | 1.18 (max 2.8, 27/180; +26/-5) |

Layout rand only: stock 1.47 / 1.03 / 1.91 / 1.12 / 1.04; rpc=1.1 2.27 (max
17.2, -10 cells) / 1.44 (-12) / 1.26 / 1.41 (-9) / 1.14; A 1.47 (+0/-0) /
1.16 (-4) / 1.91 / 1.12 / 1.06 (-1); B 1.90 (max 12.9, -4) / 1.38 (-9) / 1.39
/ 1.24 (-4) / 1.09.  Clustered layouts: stock 1.04 / 1.16 / 2.43 / 1.33 /
1.55; A 1.04 / 1.11 (+6/-1) / 2.43 / 1.33 / 1.36 (+7/-1); B 1.11 / 1.10 /
1.32 / 1.09 / 1.06.

Where the losses are on this device:

* B at cold16: td rand 250k-1M rows ORDER BY (92-100% density), plain
  chosen, 11.6-13x slower (6.7-8.2 s against 0.5-0.7 s); 2.5M rows 7.9x.
  This is what "too far" looks like on 1 ms storage.  rpc=1.1 does the same
  (max 17.2x).
* A at cold100: td rand 500k-2.5M rows ORDER BY, plain chosen, 2.2-3.7x
  (1.4-2.2 s against 0.4-1.0 s), plus two sub-10 ms cells at 1.7x.  Nothing
  at eic=16.
* Two-index cells: the stock planner picks a BitmapAnd where the
  correlated plain scan is 2-6x faster cold at eic=16 (11/45 cells); no
  model changes those (it is the CPU-side weighting again).

Full tables: `results/tables_delay.md`.

## 8. Recommendation

The committed patch ("Account for read stream latency hiding in index and
bitmap scan costs", with the page-estimate floor as its own commit): divide
the random-access penalty
(random_page_cost less seq_page_cost) of the heap pages that a read stream
fetches by the tablespace's effective_io_concurrency, for bitmap heap scans
and for plain and index-only scans of an index AM that has amgetbatch; the
first 4 pages and one page per doubling of the prefetch distance keep the
full penalty; nestloop inner scans (loop_count > 1) are untouched; and the
uncorrelated heap page estimates (plain and bitmap) are floored at the
correlated estimate, selectivity x relpages, which random order cannot
beat (the Mackert-Lohman formula assumes many tuples per page and estimates
80k pages for a scan touching 100k on a one-row-per-page table).

Why this form:

* random_page_cost=4 already assumes a cache-hit ratio in the nineties
  (the documentation: random access is far more than 4x sequential, most
  of it hits cache).  The patch changes the miss, not the hit ratio: with a
  stream, a miss overlaps with effective_io_concurrency-1 others.  Dividing
  the penalty by eic is that premise carried through; calibrating at the
  default (variant A, the first version of the patch) was inconsistent with
  it and changed nothing at the default.
* The structure of cost_index and cost_bitmap_heap_scan is right for the
  prefetching executor: the measured plain/bitmap ratio for random order
  matches the stock formulas within 10% up to 40% density on both devices
  and at both eic settings, so only the penalty's magnitude changes, and
  for both scan types alike.
* Scaling the whole per-page cost (seq_page_cost included) makes a bitmap
  scan of a whole table cheaper than a seq scan and fails select_parallel:
  the transfer part of a page's cost is not latency.
* Any model that discounts the plain scan more than the bitmap scan (the
  bitmap's sqrt(P/T) discount applied to plain scans, plain-only Little's
  law) loses 7-16 of 36 random-order cells cold by up to 4.5x (NVMe) and
  13x (1 ms), and two-index cells by up to 7.7x: the bitmap's sorted
  access is a real, eic-independent advantage of 1/(1-density) that the
  plain scan only matches when the index order has local adjacency.
* Smaller fixed discounts (penalty x 0.5, x 0.25) change almost nothing:
  the decisions move only once the penalty is nearly gone.
* The per-TID pin charge for plain scans changed no decision.

Measured effect of the committed patch (regret = time of the planner's
choice / time of the fastest measured plan; +better/-worse counts cells
changed by more than 1.5x against stock):

| cells | state | stock | patch |
|---|---|---|---|
| NVMe single-index (180) | 90% cached, eic=16 | 1.37 | 1.15 (+27/-0) |
| NVMe single-index | warm | 1.73 | 1.28 (+44/-0) |
| NVMe single-index | cold, eic=16 | 1.22 | 1.20 (+10/-4) |
| NVMe single-index | 90% cached, eic=100 | 1.43 | 1.10 (+43/-1) |
| NVMe two-index (50) | 90% cached, eic=16 | 1.20 | 1.17 (+2/-0) |
| 1 ms device single-index (180) | 90% cached, eic=16 | 1.34 | 1.27 (+15/-5) |
| 1 ms device single-index | warm | 1.70 | 1.28 (+41/-0) |
| 1 ms device single-index | cold, eic=16 | 1.41 | 1.52 (+0/-6, max 12.9) |
| 1 ms device two-index (45) | 90% cached, eic=16 | 1.02 | 1.01 (+0/-0) |

Remaining losses at 90% cached: on NVMe one cell (td rand 2M rows, no
ORDER BY, eic=100: the plain scan pins 10 TIDs per page and is 2.4x
slower); on the 1 ms device the random-order 92-100% density ORDER BY
family at 1.7-3.0x (four cells), plus five sub-15 ms cells at 1.2-1.5x
where the ramp model under-charges a tiny scan.  Cold on the 1 ms device
the same family loses 8-13x; before the patch series the equivalent
mistake cost 70x and more, which is why the bet is affordable now.

Regression suite: three expected outputs change (create_index: a
BitmapAnd/BitmapOr plan becomes a single bitmap index scan; join: two Sort
over Seq Scan plans become index scans; memoize: four filtered Seq Scans
become index scans), all on tenk1, the usual effect of a lower random page
cost on a small cached table.  create_index's test exists to show BitmapAnd
and needs a local setting (e.g. effective_io_concurrency = 0 for that
query); the other two keep their purpose.  Not updated here.

What the patch does not fix, with the evidence:

* Locally clustered index order (run4, dup64, run16): now right where the
  warm bet applies, but the planner still cannot see adjacency; a
  statistic such as block switches per index page would let it price these
  cold too.
* Jittered-but-correlated heaps (correlation 0.99999994): 1.6-6.5x against
  bitmap scans; the plain scan re-pins per row and re-requests pages inside
  the jitter window.  Same statistics gap, opposite direction.
* Selective vs sequential index (two-index cells): warm 2-23x, cold right;
  CPU-side (cpu_tuple_cost against page costs), untouched by any I/O
  model, random_page_cost=1.1 included.
* LIMIT with skewed matches: 100-1500x, the uniformity assumption.
* Pinning: at more than ~10 matching rows per page in random order the
  plain scan's per-TID pin makes it 2-3x slower than the bitmap scan even
  fully cached; the model does not charge for it.

Post material: `illustration.md` (setup `illustrate_setup.sql` and
`illustrate_setup_ios.sql`; captured EXPLAIN (ANALYZE, BUFFERS, IO) outputs in
`results/illustration/`): three range scans where the plain index scan beats
bitmap+Sort 3x cached and stays within 1.3x or ahead cold, and an index-only
scan over 40% of the pages at 100/60/40% visibility-map coverage (LP_DEAD-stub
recipe from the --ios suite, no ORDER BY so the alternative is a plain bitmap
heap scan) where the stock planner gives up on the index-only scan at 40% while
the patch keeps it: 2.4x faster fully cached, 1.7x at 90% cached, 1.3-1.4x
uncached.  The patch picks the index scan for all of them at eic 16 and 100.
