#!/usr/bin/env python3
"""
spg_concurrent_stress.py -- concurrency / VACUUM stressor for the SP-GiST
amgetbatch patch (DUT only, cassert build).

Sibling of gist_concurrent_stress.py, targeting the SP-GiST-specific code paths a
single-snapshot differential fuzzer cannot reach -- above all the TID-recycle /
visibility-map interlock between an index-only scan's pinned leaf batch and
SP-GiST VACUUM's cleanup lock (spgVacuumLockBuffer), and the SP-GiST-only hazards
GiST has no analog for:

  * moveLeafs RELOCATION: SP-GiST inserts relocate a whole leaf chain to another
    page, leaving a SPGIST_REDIRECT behind.  VACUUM revisits relocated chains via
    its pendingList and must cleanup-lock those leaf pages too -- the same
    recycling interlock the main scan applies.  We churn a low-fillfactor, wide,
    low-cardinality index hard so chains relocate constantly while an IOS holds a
    pin on the page it read its TIDs from.

  * currTuples sizing assumption: an index-only scan reconstructs the key from a
    shared by-reference prefix.  For a longValuesOK opclass (text radix) that
    prefix is unbounded, so IOS is FORBIDDEN for such opclasses (spgcanreturn);
    every opclass that CAN do IOS has a prefix that fits in a leaf tuple, so the
    fixed workspace suffices.  longvalues_forbidden_phase verifies the forbidding
    holds even for huge (over-a-page) text values, with no overflow.

The wrong-answer / assert scenario (same shape as the GiST tool):
  1. an IOS reads an SP-GiST leaf and holds a pin on it, heap TIDs + eager
     visibility info "in flight";
  2. the scanning backend is paused (SIGSTOP) so VACUUM can outpace it;
  3. UPDATE/DELETE churn makes those tuples dead, VACUUM recycles the TIDs (the
     cleanup lock is what should make it WAIT for the pin), an inserter reuses
     the freed TID;
  4. the scan resumes and trusts its stale VM reference -> wrong answer, or an
     assertion fires and the backend aborts.

SP-GiST notes: forward-only scans; NO LP_DEAD / killitems (so no LP_DEAD phase);
ordered (kNN) scans are virtual batches that hold no pin and are never
index-only, so they're a correctness check only.

Detection: server log scanned for TRAP/PANIC/assertion (a crash IS the bug),
net-zero count invariant (IOS over the whole table must stay N), per-snapshot
seq==IOS (id, reconstructed-key) cross-check, and a held-IOS-cursor (REPEATABLE
READ) count == seq truth across a VACUUM+recycle.

Run with the cassert DUT build so assertions fire.
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
from dataclasses import dataclass
from typing import Callable, List, Optional

import psycopg

import gist_fuzz_common as G

ALWAYS_STOP_KINDS = {"BACKEND_CRASH", "TIMEOUT_POSSIBLE_DEADLOCK"}


# ---------------------------------------------------------------------------
# SP-GiST opclass catalog (NOT G.build_catalog, which is GiST-shaped)
# ---------------------------------------------------------------------------

@dataclass
class SpgOpClass:
    name: str                      # short identifier / table suffix
    col_type: str                  # SQL column type
    opclass: str                   # spgist opclass name ("" = type default)
    can_return_key: bool           # spgcanreturn() true for the key column?
    recon: str                     # reconstruction path (doc only)
    value_expr: Callable[[str], str]   # SQL value expr given series alias g
    where_all: str                 # indexable predicate matching ~every row
    rand_literal: Callable[[random.Random], str]  # a SQL value literal


def _pt_value(g: str) -> str:
    # NB: value exprs go into no-parameter cur.execute(f"...") calls, so a single
    # '%' is the SQL modulo operator (psycopg only collapses '%%' with params).
    # Small coordinate space + low cardinality -> dup chains / moveLeafs churn.
    return f"point(({g} % 97)::float8, (({g} * 7) % 89)::float8)"


def _pt_literal(rng: random.Random) -> str:
    return f"point({rng.randrange(97)},{rng.randrange(89)})"


def _poly_value(g: str) -> str:
    return (f"polygon(box(point(({g} % 50)::float8, ({g} % 40)::float8), "
            f"point((({g} % 50) + 3)::float8, (({g} % 40) + 3)::float8)))")


def _poly_literal(rng: random.Random) -> str:
    a, b = rng.randrange(50), rng.randrange(40)
    return f"polygon(box(point({a},{b}),point({a + 3},{b + 3})))"


def _range_value(g: str) -> str:
    return f"int4range(({g} % 1000), ({g} % 1000) + 50)"


def _range_literal(rng: random.Random) -> str:
    a = rng.randrange(1000)
    return f"int4range({a}, {a + 50})"


# IOS-eligible opclasses only.  text (and any longValuesOK opclass) is absent on
# purpose: IOS is now forbidden for it (spgcanreturn), which longvalues_forbidden_phase
# verifies separately.  These cover the reconstruction paths that CAN do IOS:
# NULL prefix (point quad/kd), a bounded by-reference prefix (range), and a
# key-not-returnable opclass exercised INCLUDE-only (poly).
OPCLASSES: List[SpgOpClass] = [
    SpgOpClass("quadpt", "point", "quad_point_ops", True, "null-prefix",
               _pt_value, "c <@ box(point(-1,-1),point(200,200))", _pt_literal),
    SpgOpClass("kdpt", "point", "kd_point_ops", True, "null-prefix",
               _pt_value, "c <@ box(point(-1,-1),point(200,200))", _pt_literal),
    SpgOpClass("range", "int4range", "", True, "by-ref-prefix",
               _range_value, "c && int4range(0, 2000000)", _range_literal),
    SpgOpClass("poly", "polygon", "", False, "leaf-datum",
               _poly_value, "c && polygon(box(point(-1,-1),point(300,300)))",
               _poly_literal),
]


def tbl_of(oc: SpgOpClass) -> str:
    return f"spg_stress_{oc.name}"


def idx_of(oc: SpgOpClass) -> str:
    return f"spg_stress_{oc.name}_ix"


def ios_select(oc: SpgOpClass) -> str:
    """Columns to project for an index-only scan: the key too when it can be
    returned (so reconstruction is cross-checked), else just the INCLUDE id."""
    return "id, c" if oc.can_return_key else "id"


# ---------------------------------------------------------------------------

class Shared:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.failures = []
        self.fail_counts = Counter()
        self.counters = Counter()
        self.scanner_pids = {}
        self.expected = 0
        self.oc: Optional[SpgOpClass] = None
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
    G.force_scan_gucs(cur, G.IOS)


_log0 = {"size": 0}


def _is_crash(cluster):
    return G.log_shows_crash(cluster, _log0["size"]) is not None


# ---- setup ----------------------------------------------------------------

def create_index(cur, oc: SpgOpClass, fillfactor: int):
    opcl = f" {oc.opclass}" if oc.opclass else ""
    cur.execute(f"CREATE INDEX {idx_of(oc)} ON {tbl_of(oc)} "
                f"USING spgist (c{opcl}) INCLUDE (id) "
                f"WITH (fillfactor={fillfactor})")


def setup_opclass(cluster, sh: Shared, oc: SpgOpClass, n: int,
                  fillfactor: int) -> bool:
    """Build oc's table+index; return True iff an IOS plan is reachable."""
    conn = connect(cluster)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl_of(oc)}")
            cur.execute(f"CREATE TABLE {tbl_of(oc)} (id bigint, c {oc.col_type}) "
                        f"WITH (autovacuum_enabled=off, fillfactor=90)")
            cur.execute(f"INSERT INTO {tbl_of(oc)} SELECT g, {oc.value_expr('g')} "
                        f"FROM generate_series(1, {n}) g")
            create_index(cur, oc, fillfactor)
            cur.execute(f"VACUUM (FREEZE, ANALYZE) {tbl_of(oc)}")
            force_ios(cur)
            sql = f"SELECT {ios_select(oc)} FROM {tbl_of(oc)} WHERE {oc.where_all}"
            ok, reason = G.explain_gate(cur, sql, G.IOS, idx_of(oc))
            G.reset_scan_gucs(cur)
        print(f"  setup {oc.name}: n={n} ff={fillfactor} canReturn={oc.can_return_key} "
              f"IOS gate ok={ok} ({reason})")
        return ok
    finally:
        conn.close()


# ---- workers --------------------------------------------------------------

def scanner_worker(cluster, sh: Shared, oc: SpgOpClass, wid, rng):
    """Whole-table IOS; pins SP-GiST leaves and carries stale TID/visibility refs
    a concurrent recycle can invalidate.  Net-zero churn => count must == N."""
    conn = connect(cluster)
    sh.set_pid(wid, conn.info.backend_pid)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute("SET enable_indexscan_prefetch=on")
                cur.execute(f"SET effective_io_concurrency={rng.choice([4,16,64])}")
                G.evict_relation(cur, tbl_of(oc))
                force_ios(cur)
                cur.execute(f"SELECT count(*) FROM {tbl_of(oc)} WHERE {oc.where_all}")
                got = cur.fetchone()[0]
                G.reset_scan_gucs(cur)
            if got != sh.expected:
                sh.fail("IOS_COUNT_INVARIANT",
                        f"[{oc.name}] IOS count={got} expected={sh.expected}")
                if sh.stop.is_set():
                    return
            sh.bump("ios_scan")
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", f"scanner[{oc.name}]")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"scanner[{oc.name}]: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
                sh.set_pid(wid, conn.info.backend_pid)
            except Exception:  # noqa: BLE001
                return


def snapshot_crosscheck_worker(cluster, sh: Shared, oc: SpgOpClass, rng):
    """In ONE repeatable-read snapshot, compare the seqscan and index-only-scan
    answers (id and, when returnable, the reconstructed key c).  Catches missing/
    extra rows AND wrong reconstructed values -- the only structural check on
    reconstruction correctness under churn."""
    conn = psycopg.connect(**cluster.conn_params(), autocommit=False)
    sel = ios_select(oc)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                G.force_scan_gucs(cur, "seqscan")
                cur.execute(f"SELECT {sel} FROM {tbl_of(oc)} WHERE {oc.where_all}")
                truth = sorted(map(repr, cur.fetchall()))
                cur.execute("SET enable_indexscan_prefetch=on")
                cur.execute(f"SET effective_io_concurrency={rng.choice([1,8,32])}")
                G.evict_relation(cur, tbl_of(oc))
                force_ios(cur)
                cur.execute(f"SELECT {sel} FROM {tbl_of(oc)} WHERE {oc.where_all}")
                got = sorted(map(repr, cur.fetchall()))
                cur.execute("COMMIT")
            if got != truth:
                # report a small diff
                tc, gc = Counter(truth), Counter(got)
                miss = list((tc - gc).elements())[:6]
                extra = list((gc - tc).elements())[:6]
                sh.fail("SNAPSHOT_CROSSCHECK",
                        f"[{oc.name}] IOS!=seq in one snapshot; "
                        f"missing={miss} extra={extra}")
                if sh.stop.is_set():
                    return
            sh.bump("crosscheck")
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", f"crosscheck[{oc.name}]")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"crosscheck[{oc.name}]: {repr(e)[:120]}")
                return
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                try:
                    conn = psycopg.connect(**cluster.conn_params(),
                                           autocommit=False)
                except Exception:  # noqa: BLE001
                    return


def cursor_pinrace_worker(cluster, sh: Shared, oc: SpgOpClass, rng):
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
                G.force_scan_gucs(ca, "seqscan")
                ca.execute(f"SELECT count(*) FROM {tbl_of(oc)} WHERE {oc.where_all}")
                truth = ca.fetchone()[0]
                force_ios(ca)
                ca.execute(f"DECLARE cur CURSOR FOR "
                           f"SELECT {ios_select(oc)} FROM {tbl_of(oc)} "
                           f"WHERE {oc.where_all}")
                ca.execute("FETCH 1 FROM cur")
                got = ca.fetchall()
                # pin held -> churn + vacuum + recycle underneath it
                with b.cursor() as cb:
                    for _ in range(n):
                        cb.execute(f"UPDATE {tbl_of(oc)} SET c={oc.rand_literal(rng)} "
                                   f"WHERE id=%s", (rng.randrange(1, n + 1),))
                    cb.execute("SET statement_timeout='1500ms'")
                    try:
                        cb.execute(f"VACUUM {tbl_of(oc)}")
                        sh.bump("vac_proceeded")     # may be ok: pin already gone
                    except psycopg.errors.QueryCanceled:
                        sh.bump("vac_blocked")        # correct: waited for pin
                    cb.execute("RESET statement_timeout")
                    for _ in range(n):
                        cb.execute(f"UPDATE {tbl_of(oc)} SET c={oc.rand_literal(rng)} "
                                   f"WHERE id=%s", (rng.randrange(1, n + 1),))
                ca.execute("FETCH ALL FROM cur")
                got += ca.fetchall()
                ca.execute("CLOSE cur")
                ca.execute("COMMIT")
            if len(got) != truth:
                sh.fail("IOS_CURSOR_WRONG_COUNT",
                        f"[{oc.name}] held IOS cursor returned {len(got)} rows, "
                        f"snapshot truth={truth}")
                if sh.stop.is_set():
                    return
            sh.bump("cursor_race")
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", f"cursor_pinrace[{oc.name}]")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"cursor_pinrace[{oc.name}]: {repr(e)[:120]}")
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
    recycle the TIDs it still holds a stale reference to.  Always SIGCONT."""
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


def writer_worker(cluster, sh: Shared, oc: SpgOpClass, rng, lo, hi, period):
    """Net-zero UPDATE churn (c is indexed -> non-HOT) -> constant dead tuples,
    TID recycling, and frequent moveLeafs relocations, row count preserved."""
    conn = connect(cluster)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {tbl_of(oc)} SET c={oc.rand_literal(rng)} "
                            f"WHERE id=%s", (rng.randrange(lo, hi),))
        except psycopg.errors.QueryCanceled:
            sh.fail("TIMEOUT_POSSIBLE_DEADLOCK", f"writer[{oc.name}]")
            return
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"writer[{oc.name}]: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
            except Exception:  # noqa: BLE001
                return
        sh.bump("update")
        if period:
            time.sleep(period)


def recycler_worker(cluster, sh: Shared, oc: SpgOpClass, rng, lo, hi):
    """Atomic DELETE+INSERT of the same id -> frees a TID (after vacuum) that an
    inserter reuses: the 'insert a new row at the recycled TID' the race needs.
    Atomic so the count stays exactly N to every snapshot."""
    conn = psycopg.connect(**cluster.conn_params(), autocommit=False)
    while not sh.stop.is_set():
        rid = rng.randrange(lo, hi)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl_of(oc)} WHERE id=%s", (rid,))
                cur.execute(f"INSERT INTO {tbl_of(oc)} VALUES (%s, {oc.rand_literal(rng)})",
                            (rid,))
            conn.commit()
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"recycler[{oc.name}]: {repr(e)[:120]}")
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


def vacuum_worker(cluster, sh: Shared, oc: SpgOpClass, rng):
    """Aggressive, tight VACUUM loop -- must outpace the (paused) scanners to
    recycle TIDs 'in just the wrong way'."""
    conn = connect(cluster)
    while not sh.stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute(f"VACUUM {tbl_of(oc)}")
        except psycopg.errors.QueryCanceled:
            sh.bump("vacuum_canceled")
        except psycopg.Error as e:
            if _is_crash(cluster):
                sh.fail("BACKEND_CRASH", f"vacuum[{oc.name}]: {repr(e)[:120]}")
                return
            try:
                conn = connect(cluster)
            except Exception:  # noqa: BLE001
                return
        sh.bump("vacuum")


# ---- longValuesOK forbidden-IOS verification phase ------------------------

def longvalues_forbidden_phase(cluster, sh: Shared, rng, maxlen):
    """Verify the assumption the currTuples sizing rests on: index-only scans are
    FORBIDDEN for longValuesOK opclasses (text radix), so the reconstruction
    prefix -- which equals the indexed value minus the leaf suffix, and is
    unbounded for such opclasses -- can never reach the fixed per-batch workspace.

    Build a text index with values up to `maxlen` bytes (far larger than a page;
    a reconstructed prefix would massively overrun any fixed workspace), then
    confirm that NO index-only plan is ever chosen -- neither key-projecting nor
    INCLUDE-only -- that results stay correct, and that nothing overflows/asserts.
    As a control, confirm a non-longValuesOK opclass (point) still gets an IOS
    plan, so the restriction isn't overly broad."""
    conn = connect(cluster)
    tbl, idx = "spg_longval", "spg_longval_ix"
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl}")
            cur.execute(f"CREATE TABLE {tbl} (id bigint, c text) "
                        f"WITH (autovacuum_enabled=off)")
            # Values all share the 'kkkkkkkk' prefix (so the range predicate
            # below matches them) and grow to >maxlen bytes -- forcing the radix
            # opclass to suffix long values across many inner levels, exactly the
            # case that makes the reconstruction prefix unbounded.
            cur.execute(
                f"INSERT INTO {tbl} SELECT g, "
                f"repeat('k', 8) || repeat('v', (g * g * 31) % {maxlen}) || g::text "
                f"FROM generate_series(1, 400) g")
            cur.execute(f"INSERT INTO {tbl} VALUES "
                        f"(100001, repeat('k', {maxlen})), "
                        f"(100002, repeat('k', {maxlen}))")
            cur.execute(f"CREATE INDEX {idx} ON {tbl} USING spgist (c) INCLUDE (id)")
            cur.execute(f"VACUUM (FREEZE, ANALYZE) {tbl}")

            # A range predicate matching every row, including the huge ones.
            pred = "c >= 'k' AND c < 'l'"
            ok_all = True
            for sel, label in [("c", "key-projecting"), ("id", "INCLUDE-only")]:
                cur.execute("SET enable_indexscan_prefetch=on")
                cur.execute("SET effective_io_concurrency=16")
                G.evict_relation(cur, tbl)
                force_ios(cur)            # ask for IOS; the planner must refuse
                ios_ok, _why = G.explain_gate(
                    cur, f"SELECT {sel} FROM {tbl} WHERE {pred}", G.IOS, idx)
                if ios_ok:
                    ok_all = False
                    sh.fail("LONGVALUES_IOS_ALLOWED",
                            f"text IOS was planned ({label}) -- must be forbidden "
                            f"(unbounded reconstruction prefix)")
                cur.execute(f"SELECT {sel} FROM {tbl} WHERE {pred}")
                got = sorted(map(repr, cur.fetchall()))
                G.force_scan_gucs(cur, "seqscan")
                cur.execute(f"SELECT {sel} FROM {tbl} WHERE {pred}")
                truth = sorted(map(repr, cur.fetchall()))
                G.reset_scan_gucs(cur)
                if got != truth:
                    ok_all = False
                    sh.fail("LONGVALUES_WRONG_RESULT",
                            f"text {label}: {len(got)} rows vs seq {len(truth)}")
                if _is_crash(cluster):
                    sh.fail("BACKEND_CRASH", f"longvalues {label}")
                    return

            # Control: a non-longValuesOK opclass MUST still allow an IOS plan.
            cur.execute("DROP TABLE IF EXISTS spg_lv_ctl")
            cur.execute("CREATE TABLE spg_lv_ctl (id bigint, c point) "
                        "WITH (autovacuum_enabled=off)")
            cur.execute("INSERT INTO spg_lv_ctl SELECT g, point(g, g) "
                        "FROM generate_series(1, 200) g")
            cur.execute("CREATE INDEX spg_lv_ctl_ix ON spg_lv_ctl "
                        "USING spgist (c) INCLUDE (id)")
            cur.execute("VACUUM (FREEZE, ANALYZE) spg_lv_ctl")
            force_ios(cur)
            ctl_ok, ctl_why = G.explain_gate(
                cur, "SELECT id, c FROM spg_lv_ctl "
                "WHERE c <@ box(point(-1,-1),point(9999,9999))",
                G.IOS, "spg_lv_ctl_ix")
            G.reset_scan_gucs(cur)
            if not ctl_ok:
                ok_all = False
                sh.fail("LONGVALUES_CONTROL_NO_IOS",
                        f"control point opclass got no IOS plan ({ctl_why}) -- "
                        f"the longValuesOK restriction is too broad")

            if ok_all and not sh.failures:
                print(f"  longvalues: OK -- text IOS forbidden (maxlen={maxlen}), "
                      f"results correct, point control IOS-ok")
                sh.bump("longvalues_ok")
    except psycopg.Error as e:
        if _is_crash(cluster):
            sh.fail("BACKEND_CRASH", f"longvalues: {repr(e)[:160]}")
        else:
            print(f"  longvalues phase error (non-crash): {repr(e)[:160]}")
    finally:
        conn.close()


# ---- per-opclass concurrent race ------------------------------------------

def run_opclass_race(cluster, sh: Shared, oc: SpgOpClass, args, rng):
    if not setup_opclass(cluster, sh, oc, args.rows, args.fillfactor):
        print(f"  {oc.name}: IOS not reachable; skipping race")
        return
    sh.expected = args.rows
    sh.oc = oc
    with sh.lock:
        sh.scanner_pids.clear()
    _log0["size"] = G.log_size(cluster)

    print(f"  race[{oc.name}]: {args.scanners} scanners, {args.cursor_racers} "
          f"cursor-racers, {args.crosscheckers} crosscheck, {args.writers} writers, "
          f"{args.recyclers} recyclers, {args.vacuumers} vacuumers, {args.per_opclass}s")
    threads = []

    def add(fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        threads.append(t)

    for i in range(args.scanners):
        add(scanner_worker, cluster, sh, oc, f"{oc.name}{i}",
            random.Random(args.seed * 100 + i))
    for i in range(args.cursor_racers):
        add(cursor_pinrace_worker, cluster, sh, oc,
            random.Random(args.seed * 150 + i))
    for i in range(args.crosscheckers):
        add(snapshot_crosscheck_worker, cluster, sh, oc,
            random.Random(args.seed * 175 + i))
    span = max(args.rows // max(args.writers, 1), 1)
    for i in range(args.writers):
        lo = 1 + i * span
        hi = lo + span if i < args.writers - 1 else args.rows + 1
        add(writer_worker, cluster, sh, oc, random.Random(args.seed * 200 + i),
            lo, hi, args.writer_period)
    # Recyclers must own DISJOINT id ranges: two recyclers DELETE+INSERTing the
    # same id can each miss the other's INSERT (READ COMMITTED) and re-insert,
    # creating a duplicate id and breaking the net-zero count invariant.  Only
    # recyclers INSERT, so disjoint ranges keep the row count exactly N.
    rspan = max(args.rows // max(args.recyclers, 1), 1)
    for i in range(args.recyclers):
        lo = 1 + i * rspan
        hi = lo + rspan if i < args.recyclers - 1 else args.rows + 1
        add(recycler_worker, cluster, sh, oc, random.Random(args.seed * 250 + i),
            lo, hi)
    for i in range(args.vacuumers):
        add(vacuum_worker, cluster, sh, oc, random.Random(args.seed * 300 + i))
    if args.sigstop_period > 0:
        add(sigstop_worker, cluster, sh, random.Random(args.seed * 400),
            args.sigstop_period)

    for t in threads:
        t.start()

    end = time.time() + args.per_opclass
    last = 0
    while time.time() < end and not sh.stop.is_set():
        time.sleep(0.25)
        if _is_crash(cluster):
            crash = G.log_shows_crash(cluster, _log0["size"])
            sh.fail("BACKEND_CRASH",
                    (crash or "").splitlines()[0] if crash else "crash")
            break
        now = int(time.time())
        if now != last and now % 5 == 0:
            last = now
            with sh.lock:
                print(f"    [{oc.name}] t={int(end - time.time())}s "
                      f"ios={sh.counters['ios_scan']} cursor={sh.counters['cursor_race']} "
                      f"xchk={sh.counters['crosscheck']} upd={sh.counters['update']} "
                      f"rec={sh.counters['recycle']} vac={sh.counters['vacuum']} "
                      f"stop={sh.counters['sigstop']} "
                      f"vblk={sh.counters['vac_blocked']} "
                      f"vproc={sh.counters['vac_proceeded']}", flush=True)

    # signal these workers to wind down before the next opclass
    local_stop = sh.stop.is_set()
    if not local_stop:
        sh.stop.set()
    time.sleep(0.5)
    for t in threads:
        t.join(timeout=3)
    if not local_stop:
        sh.stop.clear()       # let the next opclass run, unless a real failure


# ---- driver ---------------------------------------------------------------

def run(args):
    rng = random.Random(args.seed)
    cluster = G.make_cluster("spgstress", G.DUT_BIN, args.port)
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

    if not args.skip_longvalues:
        _log0["size"] = G.log_size(cluster)
        print("running longValuesOK forbidden-IOS verification phase...")
        longvalues_forbidden_phase(cluster, sh, rng, args.longval_maxlen)
        if sh.stop.is_set():
            return _finish(sh, cluster, args)

    only = set(args.opclasses.split(",")) if args.opclasses else None
    for oc in OPCLASSES:
        if only and oc.name not in only:
            continue
        if sh.stop.is_set():
            break
        run_opclass_race(cluster, sh, oc, args, rng)

    sh.stop.set()
    return _finish(sh, cluster, args)


def _finish(sh, cluster, args):
    rc = _report(sh, cluster)
    if not args.leave_running:
        G.stop_cluster(cluster)
    return rc


def _report(sh: Shared, cluster):
    print("\n" + "=" * 70)
    print("SP-GiST concurrent stress summary")
    print("=" * 70)
    for k in sorted(sh.counters):
        print(f"  {k} = {sh.counters[k]}")
    if sh.failures:
        print(f"\nFAILURE counts by kind: {dict(sh.fail_counts)}")
        body = ["SP-GiST concurrent stress FAILURES", "",
                f"counts by kind: {dict(sh.fail_counts)}", ""]
        for kind, detail in sh.failures:
            print(f"  {kind}: {detail}")
            body.append(f"{kind}: {detail}")
        crash = G.log_shows_crash(cluster, 0)
        if crash:
            body.append("\n-- crash markers --\n" + crash)
        body.append("\n-- server log tail --\n" + G.read_log_tail(cluster, 200))
        p = G.dump_repro("spgconcstress", 0, len(sh.failures), "\n".join(body))
        print(f"repro: {p}")
        print("=" * 70 + "\nFAILURES FOUND")
        return 1
    print("=" * 70 + "\nno failures")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-opclass", type=float, default=40,
                    help="seconds of concurrent race per opclass")
    ap.add_argument("--rows", type=int, default=120,
                    help="table size -- keep SMALL (~120) for dense TID reuse; "
                         "larger tables almost never hit the race")
    ap.add_argument("--fillfactor", type=int, default=40,
                    help="index fillfactor -- low => more chains/splits/moveLeafs")
    ap.add_argument("--opclasses", default=None,
                    help="comma list to restrict (quadpt,kdpt,range,poly)")
    ap.add_argument("--scanners", type=int, default=3)
    ap.add_argument("--cursor-racers", type=int, default=2)
    ap.add_argument("--crosscheckers", type=int, default=2)
    ap.add_argument("--writers", type=int, default=6)
    ap.add_argument("--recyclers", type=int, default=2)
    ap.add_argument("--vacuumers", type=int, default=2)
    ap.add_argument("--writer-period", type=float, default=0.0)
    ap.add_argument("--sigstop-period", type=float, default=0.03,
                    help="base SIGSTOP/SIGCONT pause for scanner backends")
    ap.add_argument("--longval-maxlen", type=int, default=50000,
                    help="max text value length in the longValuesOK forbidden-IOS "
                         "phase (well over a page, so a reconstruction prefix "
                         "would overflow any fixed currTuples workspace)")
    ap.add_argument("--io-method", default=None)
    ap.add_argument("--keep-going", action="store_true",
                    help="don't stop on wrong-answer findings; push on toward "
                         "an (unexpected) assert crash")
    ap.add_argument("--skip-longvalues", action="store_true")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--leave-running", action="store_true")
    ap.add_argument("--port", type=int, default=6604)
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
