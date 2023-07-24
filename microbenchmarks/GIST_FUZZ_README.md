# GiST amgetbatch adversarial stress tools

Two complementary tools that hunt for bugs in the patch's new GiST batch scan
path (commit `cc41629c`, "Adopt amgetbatch interface in GiST index AM") by brute
force.  Run them with the **cassert** DUT build so assertions fire.

| file | what it does |
|------|--------------|
| `gist_fuzz_common.py` | shared infra: server lifecycle, opclass catalog, EXPLAIN gating, comparators, repro dump |
| `gist_diff_fuzz.py` | **differential correctness fuzzer** — DUT vs master oracle across opclasses × scan types |
| `gist_concurrent_stress.py` | **concurrency / VACUUM / killitems stressor** — DUT only, invariant-based |

Oracle = master **release** build (`/mnt/nvme/.../master/install_meson_rc/bin`,
the old `amgettuple` code).  DUT = patch **cassert** build
(`/mnt/nvme/.../patch/install_meson_dc/bin`, the new `amgetbatch` code).  Both are
PG 19beta1, so a deterministic seeded load (`setseed` + `generate_series`) builds
identical data on both.  The tools `initdb` throwaway clusters under
`/mnt/nvme/postgresql/scratch/gistfuzz/` and **refuse** to touch the persistent
`.../patch/data` / `.../master/data` regression clusters.

## Quick start

```sh
cd microbenchmarks

# differential fuzzer: 3500 queries, keep going past failures, full report
python3 gist_diff_fuzz.py --seed 1 --iterations 3500 --keep-going

# focus the recheck/lower-bound opclasses (kNN that returns only a lower bound)
python3 gist_diff_fuzz.py --opclass poly_ops,circle_ops,gist_trgm --iterations 2000

# concurrency stressor: 2 minutes, default 4 scanners / 3 churners / 1 vacuum
python3 gist_concurrent_stress.py --duration 120

# concurrency stressor against io_uring (also hunts the known deadlock class)
python3 gist_concurrent_stress.py --duration 120 --io-method io_uring
```

Nothing else needs to be running; the tools manage their own servers.  Use
`--reuse` to skip the fresh `initdb` between runs, `--leave-running` to keep the
clusters up for inspection.

## gist_diff_fuzz.py — what it checks

For each generated query (random opclass × {ordered, unordered, index-only,
bitmap} × predicate × ORDER BY distance × LIMIT × IO GUCs):

1. **EXPLAIN-gate on the DUT** — confirm the plan really uses the intended scan
   type *on the GiST index*.  A query that silently falls back to a seq scan is
   **rejected and tallied**, never counted as a pass.
2. **Evict the heap** (`pg_buffercache_evict_relation`) so the scan's heap / VM
   reads go through the **prefetch read stream** — otherwise everything is cached
   and prefetch is a no-op.
3. Run on the DUT with `debug_disable_indexscan_prefetch` **off and on**; the two must
   be **identical** (a difference is almost certainly a prefetch/read-stream bug).
4. Run the master oracle (seq-scan ground truth, always **unlimited**) and
   compare:
   * unordered / bitmap / IOS → the DUT rows are a subset of the full answer with
     the right count (LIMIT makes *which* rows arbitrary); IOS additionally
     checks the index-reconstructed values equal the heap truth;
   * ordered (kNN) → the DUT rows are the *k smallest by true distance*, in
     non-decreasing distance order; within-equal-distance reordering is reported
     **benign** (the executor guarantees no tiebreak); last-ULP distance noise is
     tolerated.

Key flags: `--keep-going` (don't stop at first failure), `--limit-fraction`
(early-termination coverage), `--grid-fraction` (tie-heavy data),
`--opclass a,b,c`, `--oracle-bin`/`--dut-bin` (set equal for a negative control;
point `--dut-bin` at the valgrind build to hunt uninitialized reads).

## gist_concurrent_stress.py — what it checks

One DUT cluster; concurrent **scanners + row churn (UPDATE/DELETE/INSERT) +
VACUUM** over a point_ops GiST index (the AM-level paths under test are
opclass-independent).  Scanners are weighted heavily toward **index-only scans**
with deep, evicted, prefetching read-ahead — the path most exposed to the
TID-recycle / visibility-map interlock that `gistunguardbatch` protects.

Invariants (any violation is a bug, with a repro dumped):

* **sentinel** rows (a region never modified) are returned *exactly* by an
  indexed scan — a missing one means a live row was lost;
* within one **REPEATABLE READ** snapshot, `seqscan == index scan == index-only
  scan` of the churned region — under concurrent VACUUM recycling TIDs;
* kNN sentinel scans stay in non-decreasing distance order;
* a **single-matching-item-per-page** phase deletes the lone match on each leaf,
  rescans (cold + prefetching) to force single-item LP_DEAD marking, and confirms
  via `pageinspect` (the master→patch behavior change).

Crash / assert / deadlock detection: per-session `statement_timeout` plus
scanning the server log for `TRAP`/`PANIC`/assertion.  Key flags: `--scanners`,
`--churners`, `--duration`, `--prefetch on|off`, `--max-eic`, `--evict-frac`,
`--io-method io_uring`.

## Output & repros

Both tools print a per-cell tally (`match` / `benign` / `reject` / `fail`) and a
final verdict.  On any mismatch / crash / timeout they write a **self-contained
repro** under `gist_fuzz_results/` containing the seed, the full DDL + data-seed
(so the exact table is reconstructable), the query, the GUCs, both result sets,
the DUT EXPLAIN, and (on a crash) the server-log tail.

## Why these two tools are complementary

The differential fuzzer is single-snapshot and read-only: it cannot reach LP_DEAD
marking, the unguard interlock, or VACUUM cleanup-lock interplay.  The concurrency
stressor targets exactly those, but has no cross-version oracle.  Together they
cover the answer-correctness surface and the concurrency surface.
```
