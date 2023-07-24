#!/usr/bin/env python3
"""Concurrent stress for hash index amgetbatch scans.

Purpose-built for the hash AM's structural hazards, none of which any live
stressor previously exercised:

  * bucket splits racing scans (fillfactor=10 + insert storms force splits;
    scans of the hot key must agree with a same-snapshot seqscan)
  * hashbucketcleanup / _hash_squeezebucket racing cached batches and
    kill-items (VACUUM loop; the LSN gate must bail, never mis-mark)
  * direction changes across a split bucket pair (SCROLL cursors doing
    MOVE FORWARD ALL / FETCH BACKWARD ALL)
  * non-MVCC guarded batches under churn (SnapshotAny scans through
    test_indexscan's index_scan_tids, which hold each batch's pin as the
    TID recycling interlock)
  * unlogged relations (--unlogged): every offset-shifting write path must
    advance the page's *fake* LSN or hashkillitemsbatch can mark the wrong
    tuple (silent corruption, caught by the invariant checker)

Oracle: within one REPEATABLE READ snapshot, a seqscan, a forced index
scan, and a forced bitmap scan of the hot key must agree exactly (multiset
of TIDs for the sentinel rows; counts for churned rows).  Any cassert TRAP,
PANIC, or assertion failure in the server log fails the run.

Fault-injection validation notes: gutting the _hash_next cross-bucket
transition branches is caught in seconds (missing rows).  Gutting the
"so->hashso_buc_split = hashpriorbatch->bucSplit" resync is NOT reliably
caught here: that bug needs a scan whose matches live only in the populated
bucket of an in-progress split (the stale flag comes from the matchless
end-of-scan probe), a geometry live splits essentially never produce for a
hot key.  Deterministic coverage for it lives in the hash_split regress
test's scroll-cursor scenario instead.

Usage:
  ./hash_concurrent_stress.py --seconds 300 [--unlogged] [--scanners 4]
"""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time

BASE = "/mnt/nvme/postgresql/patch"
# tmp_install has the test modules (test_indexscan) and injection_points;
# plain install_meson_dc does not
BINROOT = f"{BASE}/build_meson_dc/tmp_install/mnt/nvme/postgresql/patch/install_meson_dc"
BIN = f"{BINROOT}/bin"
LIB = f"{BINROOT}/lib/x86_64-linux-gnu"
SCRATCH = "/mnt/nvme/postgresql/scratch/hashstress"
PORT = 5830
HOTKEY = 1
SENTINEL = 424242
NSENTINEL = 25

stop = threading.Event()
failures = []
stats = {"scans": 0, "backward": 0, "nonmvcc": 0, "vacuums": 0,
         "inserts": 0, "deletes": 0, "checks": 0}
stats_lock = threading.Lock()


def bump(key, n=1):
    with stats_lock:
        stats[key] += n


def fail(msg):
    failures.append(msg)
    stop.set()


def run(cmd, **kw):
    env = dict(os.environ, LD_LIBRARY_PATH=LIB)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


def start_cluster():
    if os.path.exists(SCRATCH):
        run([f"{BIN}/pg_ctl", "-D", SCRATCH, "stop", "-m", "immediate"])
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH)
    r = run([f"{BIN}/initdb", "-D", SCRATCH, "-N"])
    assert r.returncode == 0, r.stderr
    r = run([f"{BIN}/pg_ctl", "-D", SCRATCH, "-o",
             f"-p {PORT} -c fsync=off -c autovacuum=off "
             "-c restart_after_crash=off -c log_min_messages=warning",
             "-l", f"{SCRATCH}/server.log", "start"])
    assert r.returncode == 0, r.stderr + r.stdout


def stop_cluster():
    run([f"{BIN}/pg_ctl", "-D", SCRATCH, "stop", "-m", "fast"])


def connect(autocommit=True):
    import psycopg
    conn = psycopg.connect(host="/tmp", port=PORT, dbname="postgres",
                           autocommit=autocommit)
    conn.execute("SET statement_timeout = '30s'")
    return conn


def setup_schema(unlogged):
    conn = connect()
    ul = "UNLOGGED " if unlogged else ""
    conn.execute("CREATE EXTENSION test_indexscan")
    conn.execute(f"""CREATE {ul}TABLE ht (v int4, pad text)
                     WITH (autovacuum_enabled = false)""")
    # fillfactor 10 makes bucket splits frequent
    conn.execute("CREATE INDEX ht_idx ON ht USING hash (v) WITH (fillfactor = 10)")
    conn.execute(f"""INSERT INTO ht
                     SELECT {SENTINEL}, 'sentinel' FROM generate_series(1, {NSENTINEL})""")
    conn.execute(f"""INSERT INTO ht
                     SELECT {HOTKEY}, 'hot' FROM generate_series(1, 200)""")
    conn.close()


def churner():
    conn = connect()
    n = 0
    while not stop.is_set():
        try:
            n += 1
            # bulk insert of random keys drives splits; hot-key inserts grow
            # the hot bucket's overflow chain
            conn.execute("""INSERT INTO ht
                            SELECT (random() * 100000)::int4 + 1000, 'x'
                              FROM generate_series(1, 500)""")
            conn.execute(f"""INSERT INTO ht
                             SELECT {HOTKEY}, 'hot' FROM generate_series(1, 50)""")
            bump("inserts", 550)
            if n % 5 == 0:
                # delete storms create squeeze/cleanup fodder; never touch
                # the sentinel
                conn.execute(f"""DELETE FROM ht
                                 WHERE ctid IN (SELECT ctid FROM ht
                                                 WHERE v <> {SENTINEL}
                                                 ORDER BY random() LIMIT 400)""")
                bump("deletes", 400)
            if n % 11 == 0:
                conn.execute(f"DELETE FROM ht WHERE v = {HOTKEY}")
                conn.execute(f"""INSERT INTO ht
                                 SELECT {HOTKEY}, 'hot' FROM generate_series(1, 100)""")
        except Exception as e:
            if not stop.is_set():
                fail(f"churner: {e!r}")
    conn.close()


def vacuumer():
    conn = connect()
    while not stop.is_set():
        try:
            conn.execute("VACUUM ht")
            bump("vacuums")
            time.sleep(0.05)
        except Exception as e:
            if not stop.is_set():
                fail(f"vacuumer: {e!r}")
    conn.close()


def scanner(i):
    conn = connect()
    conn.execute("SET enable_seqscan = off")
    conn.execute("SET enable_bitmapscan = off")
    rng = random.Random(i)
    while not stop.is_set():
        try:
            kind = rng.random()
            if kind < 0.5:
                conn.execute(f"SELECT count(*) FROM ht WHERE v = {HOTKEY}")
                bump("scans")
            elif kind < 0.8:
                # scroll cursor with a full direction reversal: forward and
                # backward passes over the bucket pair must agree
                with conn.transaction():
                    conn.execute(f"""DECLARE c SCROLL CURSOR FOR
                                     SELECT v FROM ht WHERE v = {HOTKEY}""")
                    cur = conn.execute("MOVE FORWARD ALL IN c")
                    nfwd = int(re.search(r"\d+", cur.statusmessage).group())
                    rows = conn.execute("FETCH BACKWARD ALL FROM c").fetchall()
                    conn.execute("CLOSE c")
                    if len(rows) != nfwd:
                        fail(f"scanner{i}: cursor forward {nfwd} != backward {len(rows)}")
                bump("backward")
            else:
                # kill-heavy: scanning right after churner deletes records
                # dead items, driving hashkillitemsbatch under concurrency
                conn.execute("SELECT count(*) FROM ht WHERE v = 77777")
                bump("scans")
        except Exception as e:
            if not stop.is_set():
                fail(f"scanner{i}: {e!r}")
    conn.close()


def nonmvcc_scanner():
    conn = connect()
    while not stop.is_set():
        try:
            # SnapshotAny scan through the guarded batch path (pin interlock
            # dropped only at batch release, via hashunguardbatch); count is
            # not checked (SnapshotAny sees dead rows), completing without
            # assert/crash under churn is the point
            conn.execute(
                f"SELECT count(*) FROM index_scan_tids('ht_idx', 'any', 'forward', {SENTINEL}, 1)")
            conn.execute(
                f"SELECT count(*) FROM index_scan_tids('ht_idx', 'any', 'backward', {HOTKEY}, 1)")
            bump("nonmvcc", 2)
        except Exception as e:
            if not stop.is_set():
                fail(f"nonmvcc: {e!r}")
    conn.close()


def checker():
    conn = connect()
    while not stop.is_set():
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET LOCAL enable_seqscan = on")
            conn.execute("SET LOCAL enable_indexscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = off")
            seq = conn.execute(f"""SELECT array_agg(ctid ORDER BY ctid)
                                   FROM ht WHERE v = {SENTINEL}""").fetchone()[0]
            nseq = conn.execute(
                f"SELECT count(*) FROM ht WHERE v = {HOTKEY}").fetchone()[0]
            conn.execute("SET LOCAL enable_seqscan = off")
            conn.execute("SET LOCAL enable_indexscan = on")
            idx = conn.execute(f"""SELECT array_agg(ctid ORDER BY ctid)
                                   FROM ht WHERE v = {SENTINEL}""").fetchone()[0]
            nidx = conn.execute(
                f"SELECT count(*) FROM ht WHERE v = {HOTKEY}").fetchone()[0]
            conn.execute("SET LOCAL enable_indexscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = on")
            bmp = conn.execute(f"""SELECT array_agg(ctid ORDER BY ctid)
                                   FROM ht WHERE v = {SENTINEL}""").fetchone()[0]
            conn.execute("COMMIT")
            if seq != idx or seq != bmp:
                fail(f"checker: sentinel TID mismatch seq={seq} idx={idx} bmp={bmp}")
            if len(seq or []) != NSENTINEL:
                fail(f"checker: sentinel count {len(seq or [])} != {NSENTINEL}")
            if nseq != nidx:
                fail(f"checker: hotkey count mismatch seq={nseq} idx={nidx}")
            bump("checks")
            time.sleep(0.2)
        except Exception as e:
            if not stop.is_set():
                fail(f"checker: {e!r}")
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
    conn.close()


def scan_log():
    bad = re.compile(r"TRAP|PANIC|assert|Assert|segfault|corrupt", re.I)
    benign = re.compile(r"statement timeout|canceling")
    hits = []
    with open(f"{SCRATCH}/server.log", errors="replace") as f:
        for line in f:
            if bad.search(line) and not benign.search(line):
                hits.append(line.rstrip())
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--scanners", type=int, default=4)
    ap.add_argument("--unlogged", action="store_true")
    args = ap.parse_args()

    start_cluster()
    try:
        setup_schema(args.unlogged)
        threads = [threading.Thread(target=churner, daemon=True),
                   threading.Thread(target=vacuumer, daemon=True),
                   threading.Thread(target=nonmvcc_scanner, daemon=True),
                   threading.Thread(target=checker, daemon=True)]
        threads += [threading.Thread(target=scanner, args=(i,), daemon=True)
                    for i in range(args.scanners)]
        for t in threads:
            t.start()

        deadline = time.time() + args.seconds
        while time.time() < deadline and not stop.is_set():
            time.sleep(1)
        stop.set()
        for t in threads:
            t.join(timeout=45)

        # post-run: index growth implies splits happened; verify we stressed
        # what we meant to
        conn = connect()
        npages = conn.execute("SELECT pg_relation_size('ht_idx') / 8192").fetchone()[0]
        conn.close()

        loghits = scan_log()
        print(f"stats: {stats}  index_pages={npages}")
        if npages < 20:
            print("WARNING: index barely grew; split pressure was too low")
        if loghits:
            print("SERVER LOG HITS:")
            for h in loghits[:20]:
                print("  " + h)
        if failures or loghits:
            for f_ in failures[:10]:
                print("FAILURE: " + f_)
            print(f"repro state preserved in {SCRATCH}")
            return 1
        print("CLEAN")
        return 0
    finally:
        if not failures and not scan_log():
            stop_cluster()


if __name__ == "__main__":
    sys.exit(main())
