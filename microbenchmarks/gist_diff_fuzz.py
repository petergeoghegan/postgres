#!/usr/bin/env python3
"""
gist_diff_fuzz.py -- differential correctness fuzzer for GiST amgetbatch.

Brute-force search for bugs in the patch's new GiST batch scan path by comparing
its answers against master (the old amgettuple code) as a correctness oracle,
across many opclasses and the ordered / unordered / index-only / bitmap scan
types.  Designed to run with assertions ON (the DUT is the cassert build).

For every generated query we:

  1. Pick a random opclass, scan type, predicate, ORDER BY distance, and LIMIT.
  2. EXPLAIN-gate on the DUT: confirm the plan REALLY uses the intended scan type
     on the GiST index -- a query that silently falls back to a seq scan is
     rejected and tallied, never counted as a pass.
  3. Run on the DUT twice -- debug_disable_indexscan_prefetch = off and on --
     which must produce identical results (a difference is almost certainly a
     prefetch / read-stream bug).
  4. Run the master oracle (seq scan ground truth, always unlimited) and compare:
       * unordered / bitmap / IOS -> the DUT rows are a subset of the full oracle
         answer with the right count (LIMIT makes which-rows arbitrary);
       * ordered (kNN) -> the DUT rows are the k smallest by TRUE distance, in
         non-decreasing distance order, with within-tie reordering reported as
         benign (the executor does not guarantee a tiebreak).

On any mismatch / crash / timeout a self-contained repro is written under
gist_fuzz_results/.

Oracle = master release build; DUT = patch cassert build.  See gist_fuzz_common.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter, defaultdict

import psycopg

import gist_fuzz_common as G


# ---- per-dataset / per-query parameter domains -------------------------------

SIZES = [200, 800, 2000, 8000, 20000]
FILLFACTORS = [90, 50, 10]
BUFFERINGS = ["auto", "on", "off"]
NULLFRACS = [0.0, 0.0, 0.05, 0.2]
LIMITS = [1, 2, 5, 50, 500]


def applicable_intents(oc: G.OpClass):
    intents = [G.UNORDERED, G.BITMAP]
    if oc.can_ios:
        intents.append(G.IOS)
    if oc.supports_ordered:
        intents.append(G.ORDERED)
    return intents


def pick_intent(oc: G.OpClass, rng: random.Random) -> str:
    intents = applicable_intents(oc)
    # Bias toward ORDERED for lower-bound opclasses so the recheck/reorder path
    # (polygon, circle, pg_trgm <->>) is exercised heavily.
    weights = []
    for it in intents:
        if it == G.ORDERED:
            weights.append(4 if oc.has_lower_bound else 2)
        else:
            weights.append(1)
    return rng.choices(intents, weights=weights, k=1)[0]


# ---- connection management (reconnect after a backend crash) ------------------

class Link:
    def __init__(self, cluster: G.Cluster):
        self.cluster = cluster
        self.conn = None
        self.reconnect()

    def reconnect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn = psycopg.connect(**self.cluster.conn_params(), autocommit=True)

    def cursor(self):
        return self.conn.cursor()


# ---- the DUT run: gate, then prefetch on/off ---------------------------------

IO_PERTURB = [
    None,
    {"effective_io_concurrency": "0"},
    {"effective_io_concurrency": "16"},
    {"effective_io_concurrency": "100", "io_combine_limit": "1"},
    {"effective_io_concurrency": "8", "io_combine_limit": "32"},
]


def disable_prefetch_guc_value(prefetch):
    """Map a prefetch on/off setting to the value to use for the
    debug_disable_indexscan_prefetch GUC, whose meaning is inverted (on means
    that prefetching is disabled)."""
    return "off" if str(prefetch).lower() in ("on", "true", "1") else "on"


def _prep_session(cur, intent, prefetch=None, io=None, stmt_timeout="60s"):
    cur.execute("RESET ALL")
    cur.execute(f"SET statement_timeout = '{stmt_timeout}'")
    if prefetch is not None:
        cur.execute("SET debug_disable_indexscan_prefetch = "
                    f"{disable_prefetch_guc_value(prefetch)}")
    if io:
        for k, v in io.items():
            try:
                cur.execute(f"SET {k} = {v}")
            except psycopg.Error:
                pass
    G.force_scan_gucs(cur, intent)


def dut_execute(link: Link, gq: G.GenQuery, intent: str, prefetch: str, io,
                table: str):
    with link.cursor() as cur:
        # Evict the heap so this scan's heap (and VM) reads run through the
        # prefetch read stream rather than hitting shared buffers -- otherwise
        # the prefetch / eager-unguard path is never exercised.
        G.evict_relation(cur, table)
        _prep_session(cur, intent, prefetch=prefetch, io=io)
        return G.run_rows(cur, gq.sql, gq.compare)


def oracle_execute(link: Link, gq: G.GenQuery):
    with link.cursor() as cur:
        _prep_session(cur, "seqscan")          # ground truth, unlimited
        return G.run_rows(cur, gq.oracle_sql, gq.compare)


def exact_equal(a, b) -> bool:
    """Identical rows in identical order (used for prefetch on/off)."""
    return a == b


# ---- repro dumping -----------------------------------------------------------

def make_repro(seed, iteration, ds, gq, intent, io, kind, detail,
               patch_on, patch_off, oracle, explain_json):
    lines = []
    lines.append(f"GiST amgetbatch differential fuzz FAILURE")
    lines.append(f"kind: {kind}")
    lines.append(f"detail: {detail}")
    lines.append("")
    lines.append(f"seed: {seed}")
    lines.append(f"iteration: {iteration}")
    lines.append(f"opclass: {ds['oc'].name}  intent: {intent}")
    lines.append(f"io_perturb: {io}")
    lines.append("")
    lines.append("-- dataset (run on BOTH clusters to reproduce) --")
    lines.append(f"-- nrows={ds['nrows']} fillfactor={ds['fillfactor']} "
                 f"buffering={ds['buffering']} nullfrac={ds['nullfrac']} "
                 f"grid={ds['grid']} dataset_seed={ds['seed']}")
    oc = ds['oc']
    incl = " INCLUDE (id)" if oc.can_ios else ""
    opcl = f" {oc.opclass}" if oc.opclass else ""
    val = oc.value_sql(ds['grid'])
    if ds['nullfrac'] > 0:
        val = f"CASE WHEN random() < {ds['nullfrac']} THEN NULL ELSE {val} END"
    lines.append(f"SET synchronize_seqscans = off;")
    lines.append(f"DROP TABLE IF EXISTS {G.table_name(oc)};")
    lines.append(f"CREATE TABLE {G.table_name(oc)} (id bigint, c {oc.col_type}) "
                 f"WITH (fillfactor={ds['fillfactor']});")
    lines.append(f"SELECT setseed({G.seed_to_float(ds['seed'])});")
    lines.append(f"INSERT INTO {G.table_name(oc)} SELECT g, {val} "
                 f"FROM generate_series(1,{ds['nrows']}) g;")
    lines.append(f"CREATE INDEX {G.index_name(oc)} ON {G.table_name(oc)} "
                 f"USING gist (c{opcl}){incl} WITH (buffering={ds['buffering']});")
    lines.append(f"VACUUM (FREEZE, ANALYZE) {G.table_name(oc)};")
    lines.append("")
    lines.append("-- patch query (DUT, with LIMIT if any) --")
    lines.append(gq.sql + ";")
    lines.append("")
    lines.append("-- oracle query (master seqscan, unlimited) --")
    lines.append(gq.oracle_sql + ";")
    lines.append("")
    lines.append(f"counts: patch_on={len(patch_on)} patch_off={len(patch_off)} "
                 f"oracle_full={len(oracle)}")
    lines.append("")
    lines.append("patch_on rows (first 40): " + repr(patch_on[:40]))
    lines.append("patch_off rows (first 40): " + repr(patch_off[:40]))
    lines.append("oracle rows (first 40): " + repr(oracle[:40]))
    lines.append("")
    lines.append("-- DUT EXPLAIN (FORMAT JSON) --")
    lines.append(str(explain_json))
    return "\n".join(lines)


# ---- main loop ---------------------------------------------------------------

def run(args):
    seed = args.seed
    master_rng = random.Random(seed)

    oracle_cluster = G.make_cluster("oracle", args.oracle_bin, args.oracle_port)
    dut_cluster = G.make_cluster("dut", args.dut_bin, args.dut_port)
    if args.oracle_bin == args.dut_bin:
        print("NOTE: oracle and DUT use the SAME binary (negative control) -- "
              "every check should pass.")

    print(f"[seed={seed}] bringing up clusters "
          f"(fresh={not args.reuse})...", flush=True)
    G.bring_up(oracle_cluster, fresh=not args.reuse)
    G.bring_up(dut_cluster, fresh=not args.reuse)
    print("clusters up", flush=True)

    catalog = [oc for oc in G.build_catalog()
               if not args.opclass or oc.name in args.opclass]
    if not catalog:
        print(f"no opclasses match filter {args.opclass}", file=sys.stderr)
        return 2

    oracle_link = Link(oracle_cluster)
    dut_link = Link(dut_cluster)
    oracle_load = psycopg.connect(**oracle_cluster.conn_params(), autocommit=True)
    dut_load = psycopg.connect(**dut_cluster.conn_params(), autocommit=True)

    stats = Counter()
    by_cell = defaultdict(Counter)            # (opclass,intent) -> Counter
    reject_reasons = Counter()
    repros = []

    start = time.time()
    iteration = 0
    dataset_idx = 0
    failures = 0
    deadline = start + args.duration if args.duration else None

    try:
        while True:
            if args.iterations and iteration >= args.iterations:
                break
            if deadline and time.time() >= deadline:
                break

            # --- build a fresh dataset (table identical on both clusters) ---
            oc = catalog[dataset_idx % len(catalog)]
            dataset_idx += 1
            ds = {
                "oc": oc,
                "nrows": master_rng.choice(SIZES),
                "fillfactor": master_rng.choice(FILLFACTORS),
                "buffering": master_rng.choice(BUFFERINGS),
                "nullfrac": master_rng.choice(NULLFRACS),
                "grid": master_rng.random() < args.grid_fraction,
                "seed": master_rng.randint(1, 2_000_000),
            }
            try:
                for ld in (oracle_load, dut_load):
                    G.load_table(ld, oc, ds["nrows"], ds["fillfactor"],
                                 ds["buffering"], ds["nullfrac"], ds["grid"],
                                 ds["seed"])
            except psycopg.Error as e:
                stats["load_error"] += 1
                print(f"  LOAD ERROR [{oc.name}]: {repr(e)[:160]}")
                continue

            # --- run a batch of queries against this dataset ---
            for _ in range(args.queries_per_dataset):
                if args.iterations and iteration >= args.iterations:
                    break
                if deadline and time.time() >= deadline:
                    break
                iteration += 1
                intent = pick_intent(oc, master_rng)
                limit = (master_rng.choice(LIMITS)
                         if master_rng.random() < args.limit_fraction else None)
                io = master_rng.choice(IO_PERTURB)
                gq = G.build_query(oc, intent, master_rng, ds["grid"], limit)
                cell = (oc.name, intent)
                by_cell[cell]["n"] += 1

                # gate on the DUT
                explain_json = None
                dut_log0 = G.log_size(dut_cluster)
                try:
                    with dut_link.cursor() as cur:
                        _prep_session(cur, intent)
                        ok, reason = G.explain_gate(
                            cur, gq.sql, intent, G.index_name(oc))
                        if not ok:
                            try:
                                cur.execute("EXPLAIN (FORMAT JSON) " + gq.sql)
                                explain_json = cur.fetchone()[0]
                            except psycopg.Error:
                                pass
                except psycopg.Error as e:
                    if not _handle_possible_crash(
                            dut_cluster, dut_link, dut_log0, e,
                            seed, iteration, ds, gq, intent, io, repros):
                        return 3
                    stats["error"] += 1
                    continue

                if not ok:
                    stats["gate_reject"] += 1
                    by_cell[cell]["reject"] += 1
                    reject_reasons[(oc.name, intent, reason)] += 1
                    continue
                stats["gate_ok"] += 1

                # execute DUT (prefetch on/off) and oracle
                dut_log0 = G.log_size(dut_cluster)
                tbl = G.table_name(oc)
                try:
                    patch_on = dut_execute(dut_link, gq, intent, "on", io, tbl)
                    patch_off = dut_execute(dut_link, gq, intent, "off", None, tbl)
                    oracle = oracle_execute(oracle_link, gq)
                except psycopg.errors.QueryCanceled as e:
                    failures += 1
                    stats["timeout"] += 1
                    by_cell[cell]["fail"] += 1
                    body = make_repro(seed, iteration, ds, gq, intent, io,
                                      "TIMEOUT_POSSIBLE_DEADLOCK", str(e),
                                      [], [], [], explain_json)
                    body += "\n\n-- DUT log tail --\n" + G.read_log_tail(dut_cluster)
                    p = G.dump_repro("difffuzz", seed, iteration, body)
                    repros.append(p)
                    print(f"\n  TIMEOUT [{oc.name}/{intent}] -> {p}")
                    if args.stop_on_first_failure:
                        break
                    dut_link.reconnect()
                    continue
                except psycopg.Error as e:
                    if not _handle_possible_crash(
                            dut_cluster, dut_link, dut_log0, e,
                            seed, iteration, ds, gq, intent, io, repros):
                        return 3
                    stats["error"] += 1
                    continue

                # --- compare ---
                fail_kind = fail_detail = None
                if not exact_equal(patch_on, patch_off):
                    fail_kind = "PREFETCH_ONOFF_DIVERGENCE"
                    pc, oc_ = Counter(patch_on), Counter(patch_off)
                    fail_detail = (f"on-only={list((pc-oc_).elements())[:6]} "
                                   f"off-only={list((oc_-pc).elements())[:6]} "
                                   f"(order-sensitive)")
                else:
                    res = G.compare_rows(gq, patch_on, oracle)
                    if res.status == "MATCH":
                        stats["match"] += 1
                        by_cell[cell]["match"] += 1
                    elif res.status == "BENIGN":
                        stats["benign"] += 1
                        by_cell[cell]["benign"] += 1
                        stats[f"benign:{res.kind}"] += 1
                    else:
                        fail_kind, fail_detail = res.kind, res.detail

                if fail_kind:
                    failures += 1
                    stats["fail"] += 1
                    by_cell[cell]["fail"] += 1
                    body = make_repro(seed, iteration, ds, gq, intent, io,
                                      fail_kind, fail_detail,
                                      patch_on, patch_off, oracle, explain_json)
                    p = G.dump_repro("difffuzz", seed, iteration, body)
                    repros.append(p)
                    print(f"\n  FAIL [{oc.name}/{intent}] {fail_kind}: "
                          f"{fail_detail}\n    {gq.sql}\n    repro: {p}")
                    if args.stop_on_first_failure:
                        break

                if iteration % args.report_interval == 0:
                    _progress(iteration, stats, time.time() - start)

            if args.stop_on_first_failure and failures:
                break
    finally:
        oracle_load.close()
        dut_load.close()
        _summary(stats, by_cell, reject_reasons, repros, time.time() - start)
        if not args.leave_running:
            G.stop_cluster(oracle_cluster)
            G.stop_cluster(dut_cluster)

    return 1 if failures else 0


def _handle_possible_crash(cluster, link, log0, exc, seed, iteration, ds, gq,
                           intent, io, repros) -> bool:
    """Return True if it was a transient error we can keep going from, False if
    the server crashed (and a repro was dumped)."""
    crash = G.log_shows_crash(cluster, log0)
    if crash:
        body = make_repro(seed, iteration, ds, gq, intent, io,
                          "BACKEND_CRASH", str(exc).replace("\n", " ")[:200],
                          [], [], [], None)
        body += "\n\n-- crash markers --\n" + crash
        body += "\n\n-- DUT log tail --\n" + G.read_log_tail(cluster, 120)
        p = G.dump_repro("difffuzz_crash", seed, iteration, body)
        repros.append(p)
        print(f"\n  *** BACKEND CRASH at iter {iteration} -> {p}")
        return False
    # transient (e.g. a malformed generated predicate) -- reconnect and go on
    try:
        link.reconnect()
    except Exception:  # noqa: BLE001
        return False
    return True


def _progress(iteration, stats, elapsed):
    print(f"[{iteration:6d}] match={stats['match']} benign={stats['benign']} "
          f"fail={stats['fail']} reject={stats['gate_reject']} "
          f"({iteration/max(elapsed,1e-9):.1f}/s)", flush=True)


def _summary(stats, by_cell, reject_reasons, repros, elapsed):
    print("\n" + "=" * 70)
    print(f"GiST differential fuzz summary  ({elapsed:.1f}s)")
    print("=" * 70)
    print(f"gate_ok={stats['gate_ok']} gate_reject={stats['gate_reject']} "
          f"load_error={stats['load_error']}")
    print(f"MATCH={stats['match']}  BENIGN={stats['benign']}  "
          f"FAIL={stats['fail']}  TIMEOUT={stats['timeout']}  "
          f"ERROR={stats['error']}")
    for k in sorted(stats):
        if k.startswith("benign:"):
            print(f"  {k} = {stats[k]}")
    print("\nper (opclass, intent):")
    for cell in sorted(by_cell):
        c = by_cell[cell]
        flag = "  <-- FAIL" if c.get("fail") else ""
        print(f"  {cell[0]:16s} {cell[1]:9s} n={c['n']:4d} "
              f"match={c.get('match',0):4d} benign={c.get('benign',0):3d} "
              f"reject={c.get('reject',0):3d} fail={c.get('fail',0):3d}{flag}")
    if reject_reasons:
        print("\ngate rejections:")
        for k, v in sorted(reject_reasons.items()):
            print(f"  {k}: {v}")
    if repros:
        print(f"\n{len(repros)} repro file(s):")
        for p in repros:
            print(f"  {p}")
    print("=" * 70)
    verdict = "FAILURES FOUND" if stats['fail'] or stats['timeout'] \
        else "no failures"
    print(verdict)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--iterations", type=int, default=2000,
                    help="total queries (0 = unbounded; use --duration)")
    ap.add_argument("--duration", type=float, default=0,
                    help="time budget in seconds (0 = use --iterations)")
    ap.add_argument("--queries-per-dataset", type=int, default=25)
    ap.add_argument("--opclass", type=lambda s: set(s.split(",")), default=None,
                    help="comma-separated opclass names to restrict to")
    ap.add_argument("--limit-fraction", type=float, default=0.33,
                    help="fraction of queries that get a LIMIT (early-termination "
                         "coverage); the rest are unlimited full-set checks")
    ap.add_argument("--grid-fraction", type=float, default=0.4,
                    help="fraction of datasets using tie-heavy grid-snapped data")
    ap.add_argument("--report-interval", type=int, default=200)
    ap.add_argument("--stop-on-first-failure", action="store_true", default=True)
    ap.add_argument("--keep-going", dest="stop_on_first_failure",
                    action="store_false")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse existing scratch clusters instead of fresh initdb")
    ap.add_argument("--leave-running", action="store_true",
                    help="do not stop the clusters at the end")
    ap.add_argument("--oracle-port", type=int, default=6601)
    ap.add_argument("--dut-port", type=int, default=6602)
    ap.add_argument("--oracle-bin", default=G.ORACLE_BIN,
                    help="oracle bin dir (default master release)")
    ap.add_argument("--dut-bin", default=G.DUT_BIN,
                    help="DUT bin dir (default patch cassert; "
                         "set to the oracle bin for a negative control, or to "
                         "the valgrind build to hunt uninitialized reads)")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
