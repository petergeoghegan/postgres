#!/usr/bin/env python3
"""
gist_concurrent_stress.py -- concurrency / VACUUM / killitems stressor for the
GiST amgetbatch patch (DUT only, cassert build).

This targets the code paths a single-snapshot differential fuzzer structurally
cannot reach -- in particular the TID-recycle / visibility-map interlock between
an index-only scan's pinned batch and GiST VACUUM's cleanup lock on index leaf
pages (gistvacuumpage).  The wrong-answer / assert scenario it provokes:

  1. an index-only scan reads a GiST leaf and holds a pin on it, with heap TIDs
     and eagerly-captured visibility-cache info "in flight";
  2. the scanning backend is paused mid-scan (SIGSTOP) -- so VACUUM can outpace
     it;
  3. concurrent UPDATE/DELETE churn makes those tuples dead, VACUUM recycles the
     TIDs (the cleanup lock is what should make it WAIT for the pin), and an
     inserter reuses the freed TID for a brand-new row;
  4. the scan resumes and consults its stale reference -> wrong answer, or the
     assertion HEAP_BATCH_VIS_CACHED fires and the backend aborts.

The headline pause mechanism is SIGSTOP/SIGCONT on a random scanner backend --
no injection points required, and general enough to surface timing bugs nobody
anticipated.  A deterministic held-IOS-cursor worker complements it.

Detection: server log scanned for TRAP/PANIC/assertion (a crash IS the bug),
plus a net-zero count invariant (IOS count over the whole table must stay N) and
a per-snapshot index==seq==index-only cross-check.

Run with the cassert DUT build so the assertion fires.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import threading
import time
from collections import Counter

import psycopg

import gist_fuzz_common as G

TABLE = "gist_stress"
INDEX = "gist_stress_gix"

# Small table + small coordinate box -> few leaf/heap pages -> dense TID reuse,
# which is what makes the recycle race likely (a big table almost never hits it).
COORD = 100
BOX_ALL = f"box(point(-1,-1),point({COORD+1},{COORD+1}))"   # matches every row


# A crash or a deadlock always halts the run; wrong-answers can be accumulated in
# --keep-going mode so the run pushes on toward the (unexpected) assert crash.
ALWAYS_STOP_KINDS = {"BACKEND_CRASH", "TIMEOUT_POSSIBLE_DEADLOCK"}


class Shared:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.failures = []
        self.fail_counts = Counter()
        self.counters = Counter()
        self.scanner_pids = {}        # worker_id -> backend pid (for SIGSTOP)
        self.expected = 0
        self.keep_going = False

    def fail(self, kind, detail):
        with self.lock:
            self.fail_counts[kind] += 1
            if len(self.failures) < 50:
                self.failures.append((kind, detail))
            if kind in ALWAYS_STOP_KINDS or not self.keep_going:
                self.stop.set()

    def bump(self, key, n=1):
        with self.lock:
            self.counters[key] += n

    def set_pid(self, wid, pid):
        with self.lock:
            self.scanner_pids[wid] = pid

    def random_pid(self, rng):
        with self.lock:
            pids = list(self.scanner_pids.values())
        return rng.choice(pids) if pids else None


def connect(cluster):
    c = psycopg.connect(**cluster.conn_params(), autocommit=True)
    with c.cursor() as cur:
        cur.execute("SET statement_timeout = '30s'")
        cur.execute("SET max_parallel_workers_per_gather = 0")
    return c


def force_ios(cur):
    cur.execute("SET enable_seqscan=off; SET enable_bitmapscan=off; "
                "SET enable_indexscan=off; SET enable_indexonlyscan=on")


def rnd_point(rng):
    return f"point({round(rng.uniform(0,COORD),3)},{round(rng.uniform(0,COORD),3)})"


_log0 = {"size": 0}


def _is_crash(cluster):
    return G.log_shows_crash(cluster, _log0["size"]) is not None


# ----------------------------------------------------------------------------

def setup(cluster, sh: Shared, n):
    conn = connect(cluster)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"CREATE TABLE {TABLE} (id bigint, c point) "
                    f"WITH (autovacuum_enabled=off, fillfactor=90)")
        cur.execute(f"INSERT INTO {TABLE} SELECT g, "
                    f"point(random()*{COORD}, random()*{COORD}) "
                    f"FROM generate_series(1, {n}) g")
        # low index fillfactor -> more leaves / splits -> more vacuum work
        cur.execute(f"CREATE INDEX {INDEX} ON {TABLE} USING gist (c) "
                    f"INCLUDE (id) WITH (fillfactor=50)")
        cur.execute(f"VACUUM (FREEZE, ANALYZE) {TABLE}")
        force_ios(cur)
        ok, reason = G.explain_gate(
            cur, f"SELECT id FROM {TABLE} WHERE c <@ {BOX_ALL}", G.IOS, INDEX)
        G.reset_scan_gucs(cur)
    sh.expected = n
    print(f"setup: {n} rows; IOS gate ok={ok} ({reason})")
    conn.close()


# ---- workers ----------------------------------------------------------------

def scanner_worker(cluster, sh: Shared, wid, rng):
    """Index-only scan over the whole table; pins gist leaves and carries stale
    TID/visibility references that a concurrent recycle can invalidate.  Deep
    prefetch + cold heap widen the in-flight window.  Net-zero churn means the
    visible count must always be exactly `expected`."""
    conn = connect(cluster)
    sh.set_pid(wid, conn.info.backend_pid)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute("SET enable_indexscan_prefetch=on")
                cur.execute(f"SET effective_io_concurrency={rng.choice([4,16,64])}")
                G.evict_relation(cur, TABLE)
                force_ios(cur)
                cur.execute(f"SELECT count(*) FROM {TABLE} WHERE c <@ {BOX_ALL}")
                got = cur.fetchone()[0]
                G.reset_scan_gucs(cur)
            if got != sh.expected:
                sh.fail("IOS_COUNT_INVARIANT",
                        f"IOS count={got} expected={sh.expected}")
                if sh.stop.is_set():
                    return
            sh.bump("ios_scan")
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", "scanner")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"scanner: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
                sh.set_pid(wid, conn.info.backend_pid)
            except Exception:  # noqa: BLE001
                return


def cursor_pinrace_worker(cluster, sh: Shared, rng):
    """Deterministic pin holder: hold an IOS cursor mid-scan across a VACUUM +
    recycle.  In a REPEATABLE READ snapshot the held cursor must return exactly
    as many rows as a seq scan of the same snapshot."""
    a = psycopg.connect(**cluster.conn_params(), autocommit=False)
    b = connect(cluster)
    n = sh.expected
    while not sh.stop.is_set():
        try:
            with a.cursor() as ca:
                ca.execute("SET enable_indexscan_prefetch=on")
                ca.execute("SET effective_io_concurrency=16")
                ca.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                ca.execute("SET enable_seqscan=on; SET enable_indexscan=off; "
                           "SET enable_bitmapscan=off; SET enable_indexonlyscan=off")
                ca.execute(f"SELECT count(*) FROM {TABLE} WHERE c <@ {BOX_ALL}")
                truth = ca.fetchone()[0]
                force_ios(ca)
                ca.execute(f"DECLARE cur CURSOR FOR "
                           f"SELECT id FROM {TABLE} WHERE c <@ {BOX_ALL}")
                ca.execute("FETCH 1 FROM cur")
                got = ca.fetchall()
                # pin held -> churn + vacuum + recycle underneath it
                with b.cursor() as cb:
                    for _ in range(n):
                        cb.execute(f"UPDATE {TABLE} SET c={rnd_point(rng)} "
                                   f"WHERE id=%s", (rng.randrange(1, n+1),))
                    cb.execute("SET statement_timeout='1200ms'")
                    try:
                        cb.execute(f"VACUUM {TABLE}")
                        sh.bump("vac_proceeded")     # bug: didn't wait for pin
                    except psycopg.errors.QueryCanceled:
                        sh.bump("vac_blocked")        # correct: waited for pin
                    cb.execute("RESET statement_timeout")
                    for _ in range(n):
                        cb.execute(f"UPDATE {TABLE} SET c={rnd_point(rng)} "
                                   f"WHERE id=%s", (rng.randrange(1, n+1),))
                ca.execute("FETCH ALL FROM cur")
                got += ca.fetchall()
                ca.execute("CLOSE cur")
                ca.execute("COMMIT")
            if len(got) != truth:
                sh.fail("IOS_CURSOR_WRONG_COUNT",
                        f"held IOS cursor returned {len(got)} rows, "
                        f"snapshot truth={truth}")
                if sh.stop.is_set():
                    return
            sh.bump("cursor_race")
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", "cursor_pinrace")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"cursor_pinrace: {repr(e)[:120]}")
                return
            try:
                a.rollback()
            except Exception:  # noqa: BLE001
                try:
                    a = psycopg.connect(**cluster.conn_params(), autocommit=False)
                    b = connect(cluster)
                except Exception:  # noqa: BLE001
                    return


def sigstop_worker(cluster, sh: Shared, rng, period):
    """Randomly pause a scanner backend mid-scan so VACUUM can outpace it and
    recycle the TIDs it still holds a stale reference to.  General chaos: also
    surfaces timing bugs nobody anticipated.  Always SIGCONT (finally)."""
    while not sh.stop.is_set():
        pid = sh.random_pid(rng)
        if pid is None:
            time.sleep(0.05)
            continue
        try:
            os.kill(pid, signal.SIGSTOP)
            sh.bump("sigstop")
            time.sleep(period * rng.uniform(0.5, 3.0))
        except ProcessLookupError:
            continue
        finally:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        time.sleep(period * rng.uniform(0.2, 1.0))


def writer_worker(cluster, sh: Shared, rng, lo, hi, period):
    """Net-zero UPDATE churn (non-HOT: c is the indexed column) -> constant dead
    tuples + TID recycling, with the row count preserved."""
    conn = connect(cluster)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {TABLE} SET c={rnd_point(rng)} WHERE id=%s",
                            (rng.randrange(lo, hi),))
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", "writer")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"writer: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
            except Exception:  # noqa: BLE001
                return
        sh.bump("update")
        if period:
            time.sleep(period)


def recycler_worker(cluster, sh: Shared, rng, lo, hi):
    """Atomic DELETE+INSERT of the same id -> frees a TID (after vacuum) and an
    inserter reuses it: the 'insert a new row at the recycled TID' the race
    needs.  Atomic so the count stays exactly N to every snapshot."""
    conn = psycopg.connect(**cluster.conn_params(), autocommit=False)
    while not sh.stop.is_set():
        rid = rng.randrange(lo, hi)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {TABLE} WHERE id=%s", (rid,))
                cur.execute(f"INSERT INTO {TABLE} VALUES (%s, {rnd_point(rng)})",
                            (rid,))
            conn.commit()
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"recycler: {repr(e)[:120]}")
                return
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                try:
                    conn = psycopg.connect(**cluster.conn_params(),
                                           autocommit=False)
                except Exception:  # noqa: BLE001
                    return
        sh.bump("recycle")


def vacuum_worker(cluster, sh: Shared, rng):
    """Aggressive, tight VACUUM loop -- must outpace the (paused) scanners to
    recycle TIDs 'in just the wrong way'."""
    conn = connect(cluster)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute(f"VACUUM {TABLE}")
        except psycopg.errors.QueryCanceled:
            sh.bump("vacuum_canceled")
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"vacuum: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
            except Exception:  # noqa: BLE001
                return
        sh.bump("vacuum")


# ---- single-item-per-page LP_DEAD phase ------------------------------------

def single_item_lp_dead_phase(cluster, sh: Shared, rng):
    conn = connect(cluster)
    tbl, idx = "gist_sparse", "gist_sparse_gix"
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl}")
            cur.execute(f"CREATE TABLE {tbl} (id bigint, c point) "
                        f"WITH (fillfactor=10, autovacuum_enabled=off)")
            cur.execute(f"INSERT INTO {tbl} "
                        f"SELECT g, point(random()*1000, random()*1000) "
                        f"FROM generate_series(1, 8000) g")
            cur.execute(f"CREATE INDEX {idx} ON {tbl} USING gist (c) "
                        f"WITH (fillfactor=10)")
            cur.execute(f"VACUUM (FREEZE, ANALYZE) {tbl}")
            pred = "c <@ box(point(0,0),point(200,200))"
            G.force_scan_gucs(cur, G.UNORDERED)
            cur.execute(f"SELECT id FROM {tbl} WHERE {pred}")
            before = {r[0] for r in cur.fetchall()}
            G.reset_scan_gucs(cur)
            if not before:
                print("single-item phase: predicate matched 0 rows, skipping")
                return
            cur.execute(f"DELETE FROM {tbl} WHERE id = ANY(%s)", (list(before),))
            try:
                cur.execute(f"SET enable_indexscan_prefetch = on")
                cur.execute("SET effective_io_concurrency = 32")
            except psycopg.Error:
                pass
            for _ in range(3):
                G.evict_relation(cur, tbl)
                G.force_scan_gucs(cur, G.UNORDERED)
                cur.execute(f"SELECT id FROM {tbl} WHERE {pred}")
                after = {r[0] for r in cur.fetchall()}
                G.reset_scan_gucs(cur)
                if after:
                    sh.fail("SINGLE_ITEM_DEAD_ROWS_RETURNED",
                            f"deleted rows still visible: {sorted(after)[:12]}")
                    return
            dead = _count_dead_gist_items(cur, idx)
            if dead is None:
                print("single-item phase: pageinspect unavailable; no crash")
            elif dead == 0:
                print("single-item phase: WARNING no LP_DEAD items observed")
            else:
                print(f"single-item phase: confirmed {dead} LP_DEAD items marked")
            sh.bump("single_item_ok")
    except psycopg.Error as e:
        if _is_crash(cluster):
            sh.fail("BACKEND_CRASH", f"single-item: {repr(e)[:160]}")
        else:
            print(f"single-item phase error (non-crash): {repr(e)[:160]}")
    finally:
        conn.close()


def _count_dead_gist_items(cur, idx):
    try:
        cur.execute("SELECT relpages FROM pg_class WHERE relname = %s", (idx,))
        npages = cur.fetchone()[0]
        total = 0
        for blk in range(1, npages):
            try:
                cur.execute(
                    "SELECT count(*) FROM gist_page_items("
                    "get_raw_page(%s, %s), %s::regclass) WHERE dead",
                    (idx, blk, idx))
                row = cur.fetchone()
                total += row[0] if row and row[0] else 0
            except psycopg.Error:
                return None
        return total
    except psycopg.Error:
        return None


# ---- driver -----------------------------------------------------------------

def run(args):
    rng = random.Random(args.seed)
    cluster = G.make_cluster("stress", G.DUT_BIN, args.port)
    extra = []
    if args.io_method:
        extra.append(f"--io_method={args.io_method}")
    print(f"[seed={args.seed}] bringing up DUT cluster "
          f"(io_method={args.io_method or 'default'}, rows={args.rows})...",
          flush=True)
    if not args.reuse:
        G.initdb_fresh(cluster)
    G.start_cluster(cluster, extra_opts=extra)
    G.bootstrap_db(cluster)

    sh = Shared()
    sh.keep_going = args.keep_going
    if not args.skip_single_item:
        _log0["size"] = G.log_size(cluster)
        setup(cluster, sh, n=4000)
        print("running single-item LP_DEAD phase...")
        single_item_lp_dead_phase(cluster, sh, rng)
        if sh.failures:
            return _finish(sh, cluster, args)

    setup(cluster, sh, n=args.rows)
    _log0["size"] = G.log_size(cluster)

    print(f"concurrent race: {args.scanners} scanners, {args.cursor_racers} "
          f"cursor-racers, {args.writers} writers, {args.recyclers} recyclers, "
          f"{args.vacuumers} vacuumers, SIGSTOP chaos, {args.duration}s")
    threads = []
    for i in range(args.scanners):
        threads.append(threading.Thread(
            target=scanner_worker,
            args=(cluster, sh, i, random.Random(args.seed*100+i)), daemon=True))
    for i in range(args.cursor_racers):
        threads.append(threading.Thread(
            target=cursor_pinrace_worker,
            args=(cluster, sh, random.Random(args.seed*150+i)), daemon=True))
    span = max(args.rows // max(args.writers, 1), 1)
    for i in range(args.writers):
        lo = 1 + i*span
        hi = lo + span if i < args.writers-1 else args.rows+1
        threads.append(threading.Thread(
            target=writer_worker,
            args=(cluster, sh, random.Random(args.seed*200+i), lo, hi,
                  args.writer_period), daemon=True))
    for i in range(args.recyclers):
        threads.append(threading.Thread(
            target=recycler_worker,
            args=(cluster, sh, random.Random(args.seed*250+i), 1, args.rows+1),
            daemon=True))
    for i in range(args.vacuumers):
        threads.append(threading.Thread(
            target=vacuum_worker,
            args=(cluster, sh, random.Random(args.seed*300+i)), daemon=True))
    if args.sigstop_period > 0:
        threads.append(threading.Thread(
            target=sigstop_worker,
            args=(cluster, sh, random.Random(args.seed*400), args.sigstop_period),
            daemon=True))

    for t in threads:
        t.start()

    end = time.time() + args.duration
    last = 0
    while time.time() < end and not sh.stop.is_set():
        time.sleep(0.25)
        if _is_crash(cluster):
            crash = G.log_shows_crash(cluster, _log0["size"])
            sh.fail("BACKEND_CRASH", (crash or "").splitlines()[0] if crash else "crash")
            break
        now = int(time.time())
        if now != last and now % 5 == 0:
            last = now
            with sh.lock:
                print(f"  t={int(end-time.time())}s  ios={sh.counters['ios_scan']} "
                      f"cursor={sh.counters['cursor_race']} "
                      f"upd={sh.counters['update']} rec={sh.counters['recycle']} "
                      f"vac={sh.counters['vacuum']} "
                      f"stop={sh.counters['sigstop']} "
                      f"vac_proceeded={sh.counters['vac_proceeded']}", flush=True)
    sh.stop.set()
    time.sleep(0.5)
    for t in threads:
        t.join(timeout=3)
    return _finish(sh, cluster, args)


def _finish(sh, cluster, args):
    rc = _report(sh, cluster)
    if not args.leave_running:
        G.stop_cluster(cluster)
    return rc


def _report(sh: Shared, cluster):
    print("\n" + "=" * 70)
    print("GiST concurrent stress summary")
    print("=" * 70)
    for k in sorted(sh.counters):
        print(f"  {k} = {sh.counters[k]}")
    if sh.failures:
        print(f"\nFAILURE counts by kind: {dict(sh.fail_counts)}")
        body = ["GiST concurrent stress FAILURES", "",
                f"counts by kind: {dict(sh.fail_counts)}", ""]
        for kind, detail in sh.failures:
            print(f"  {kind}: {detail}")
            body.append(f"{kind}: {detail}")
        crash = G.log_shows_crash(cluster, 0)
        if crash:
            body.append("\n-- crash markers --\n" + crash)
        body.append("\n-- server log tail --\n" + G.read_log_tail(cluster, 200))
        p = G.dump_repro("concstress", 0, len(sh.failures), "\n".join(body))
        print(f"repro: {p}")
        print("=" * 70 + "\nFAILURES FOUND")
        return 1
    print("=" * 70 + "\nno failures")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--rows", type=int, default=120,
                    help="table size -- keep SMALL (~120) for dense TID reuse; "
                         "larger tables almost never hit the race")
    ap.add_argument("--scanners", type=int, default=3)
    ap.add_argument("--cursor-racers", type=int, default=3)
    ap.add_argument("--writers", type=int, default=6)
    ap.add_argument("--recyclers", type=int, default=0,
                    help="extra DELETE+INSERT recyclers (general chaos; can "
                         "dilute the specific IOS/VACUUM assert, off by default)")
    ap.add_argument("--vacuumers", type=int, default=2)
    ap.add_argument("--writer-period", type=float, default=0.0)
    ap.add_argument("--sigstop-period", type=float, default=0.03,
                    help="base SIGSTOP/SIGCONT pause for scanner backends")
    ap.add_argument("--io-method", default=None)
    ap.add_argument("--keep-going", action="store_true",
                    help="don't stop on wrong-answer findings; push on toward the "
                         "(unexpected) HEAP_BATCH_VIS_CACHED assert crash")
    ap.add_argument("--skip-single-item", action="store_true")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--leave-running", action="store_true")
    ap.add_argument("--port", type=int, default=6603)
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
