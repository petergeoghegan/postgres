#!/usr/bin/env python3
"""
Adversarial stress test: index-scan prefetch leaf-page over-read (IoS + plain).

This is a PURE STRESS TEST (not a benchmark). It constructs something close to
the worst case for a specific weakness in index-scan I/O prefetching and
dynamically adjusts parameters to make the INDEX_SCAN_MAX_BATCHES backstop look
maximally insufficient. Its output informs later mitigation work; it implements
no mitigation itself.  Two variants (--variant ios|plain|both, default both).

==============================================================================
THE WEAKNESS (heapam_indexscan.c, branch index-prefetch-v0.63)
==============================================================================
Index-scan prefetching arms only after INDEX_PREFETCH_BLKSWITCH_THRESHOLD (=4,
"T") distinct heap-block fetches.  Once armed, the read-stream callback
heapam_index_prefetch_next_block races a prefetchPos ahead of the executor's
scanPos, loading one index LEAF PAGE per batch (amgetbatch).  The batch ring
holds up to INDEX_SCAN_MAX_BATCHES (=64); the callback only PAUSES when the ring
is full (BATCH_POS_RING_FULL).  In the callback's for(;;) loop an all-visible
item hits `continue` -- it keeps advancing prefetchPos, loading leaf page after
leaf page, WITHOUT returning a block.  So an index-only scan that does T heap
fetches at the start (arming) and then enters a long run of all-visible pages
churns through up to ~63 ("B" = INDEX_SCAN_MAX_BATCHES-1) leaf pages in a single
callback invocation before the backstop stops it.  A LIMIT N query that only
needs the first leaf page never gets to veto this: the read stream stops only at
the ring-full backstop, ~63 batches too late.

==============================================================================
PLAIN INDEX SCAN VARIANT (--variant plain)
==============================================================================
The pathology is NOT unique to index-only scans.  For a plain Index Scan the
same callback over-reads via a DIFFERENT `continue` (heapam_indexscan.c:1634:
"must not return the same heap block twice in succession").  When the read stream
asks the callback for the next block to prefetch, the callback churns through
batches whose items all point to the SAME heap block -- returning nothing -- until
it finds a different block.  That churn is bounded ONLY by INDEX_SCAN_MAX_BATCHES
and by how many index entries can point at one heap block (i.e. the max items per
heap page); the prefetch DISTANCE is irrelevant.  Construction (table pscan_stress,
composite index (a,b), a UNIQUE so it is a full Index Scan with b=K an Index Cond,
not a skip scan): a few WIDE arming rows on distinct heap blocks, then a long run
of narrow b=K rows clustered on ONE heap block but spread ~1 per leaf page (gap=0),
with b=0 filler in between.  Verified with pageinspect (bt_page_stats to size the
spacing; bt_page_items to confirm each run-region leaf page holds ~1 entry pointing
to the run heap block).  The plain case is FAR more parameter-sensitive than IoS:
the run-churn must line up to begin around when the scan has already produced its
LIMIT rows (IoS churns to ring-full in one callback call at LIMIT 1; plain peaks
near LIMIT ~ T+8 for B~63, with a much milder waste ratio).  The
--plain-nomatch-gap knob (default OFF) makes the buffer over-read exceed the ring
via no-match leaf pages that _bt_readnextpage scans but never batches -- but that
amplification is partly INHERENT to any index read-ahead under a LIMIT (an accepted
risk, NOT a distinct bug), which is why it defaults off.

==============================================================================
ASSUMPTIONS THAT PRODUCE THE PATHOLOGY (maintenance guide)
==============================================================================
The over-read only occurs while the prefetch design holds the properties below.
Wherever possible the script ACTIVELY CHECKS them so a future design change makes
the test react visibly (failed assertion / no arming / lost plateau / collapsed
over-read) instead of silently passing.  Several of these, when broken by a
deliberate *mitigation*, should make the measured over-read drop -- that is the
test correctly reporting the pathology is fixed.

 1. Arming is gated by a small count of distinct heap-block fetches.  [checked by
    T-calibration: a non-zero T is discovered]
 2. All-visible IoS items do NOT increment the arming counter (it lives in
    heapam_index_heap_fetch, skipped for all-visible items), so the all-visible
    tail neither arms nor re-arms.
 3. The callback advances prefetchPos through all-visible items (`continue`)
    without returning a block, stopping only at the ring-full backstop.  [the
    core pathology; measured directly by extra_leaf_reads saturating at B]
 4. One batch == one index leaf page (amgetbatch reads the next leaf), so extra
    batches == extra leaf-page reads.  [basis of the buffer-delta metric]
 5. The churn is synchronous on the executor's critical path (callback runs
    inside read_stream_next_buffer during the T-th heap fetch) -> wall-clock
    penalty, not just wasted I/O.
 6. LIMIT / early-termination is invisible to the read stream, so it reads ahead
    to the backstop regardless.  [the user-facing harm]
 7. The all-visible run is long (tail > ring) and uninterrupted within the ring
    window.  [guaranteed by verify_layout]
 8. Repeatability: heap LP_DEAD stubs keep the prefix non-all-visible permanently
    (no opportunistic VM set), and the posting-list trick keeps the arming index
    tuples unkillable by btkillitemsbatch.  [checked by verify_layout + re-run]
 9. Prefetch is eligible: plain MVCC-snapshot IoS with
    debug_disable_indexscan_prefetch=off.  [forced plan + GUCs]

==============================================================================
THE TWO DISTINCT LP_DEAD CONCEPTS (do not conflate)
==============================================================================
 * Heap LP_DEAD stub line pointers (in heap pages) keep the first pages
   non-all-visible, forcing the heap fetches that arm prefetching.  Created by a
   committed DELETE + VACUUM (INDEX_CLEANUP off) with autovacuum off;
   heap_page_prune_opt can never promote an LP_DEAD-stub page to all-visible.
 * Index-tuple LP_DEAD bits (set by btkillitemsbatch) are a *different* flag.  If
   the index tuples that drive the arming heap fetches got killed, subsequent
   scans would skip them and the test would stop arming.  We defeat this with a
   posting list (btree deduplication) that contains at least one TID to a live
   tuple: btkillitemsbatch can only LP_DEAD-mark a posting-list tuple when ALL
   its TIDs are reported dead, so one never-dead TID makes it permanently
   unkillable -- letting the dead-stub TIDs drive the arming switches repeatably,
   even at LIMIT 1.

==============================================================================
LAYOUT (posting-list construction)
==============================================================================
One UNLOGGED table `ios_stress(k bigint, pad bytea STORAGE PLAIN)`:
 * Dirty prefix -- `num_dirty` rows all with key k=0, each ~one-per-heap-page
   (wide pad).  After a committed DELETE of all but the highest-ctid "anchor",
   pages 0..num_dirty-2 are LP_DEAD-stub (non-all-visible) and the anchor page is
   all-visible.  Deduplication makes all k=0 TIDs one posting list; the live
   anchor TID keeps it unkillable.  Scanning k=0 drives D_switch =
   (num_dirty - 1) heap-block switches via the dead-stub TIDs *before* the anchor
   row is returned -- so even LIMIT 1 arms prefetching once D_switch >= T.
 * All-visible tail -- `n_tail` rows with unique increasing keys k=1..n_tail and
   tiny pad.  Their index leaf pages (>> B) are what the callback churns through.

Query under test:  SELECT k FROM ios_stress ORDER BY k LIMIT N   (forced IoS).

==============================================================================
METRIC
==============================================================================
extra_leaf_reads = index-leaf buffers(prefetch on) - index-leaf buffers(off),
measured on the identical query and cache state.  Index-scoped via
pg_statio_user_indexes deltas (primary) and cross-checked against the EXPLAIN
(ANALYZE, BUFFERS) Index Only Scan node delta (heap fetches cancel: asserted
equal on/off).  Taken cached (deterministic) plus uncached (real I/O).  The
arming threshold T and effective backstop B are DISCOVERED at runtime, so nothing
assumes the literal 4/64.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

import psycopg

# ---------------------------------------------------------------------------
# Self-contained configuration (this script intentionally does NOT import
# prefetch_benchmark; it only borrows its patterns so it can stand alone).
# ---------------------------------------------------------------------------

PATCH_ROOT = "/mnt/nvme/postgresql/patch"
SOURCE_DIR = os.path.join(PATCH_ROOT, "source")
BUILD_BIN = {
    "rc": os.path.join(PATCH_ROOT, "install_meson_rc", "bin"),  # release: timing
    # cassert build for the assertion hunt: always the user's own install dir,
    # NEVER a build_meson_*/tmp_install left behind by `meson test` (that can be a
    # stale/broken WIP build -- e.g. it may even Assert during initdb).  If the
    # GUC is missing, refresh it first: `meson install -C build_meson_dc`.
    "dc": os.path.join(PATCH_ROOT, "install_meson_dc", "bin"),
    # master release (no prefetch GUC) -- for side-by-side EXPLAIN comparison.
    "master": "/mnt/nvme/postgresql/master/install_meson_rc/bin",
}

# Source locations of the two tuning constants, for the cross-check.
SRC_MAX_BATCHES = os.path.join(SOURCE_DIR, "src/include/access/relscan.h")
SRC_BLKSWITCH = os.path.join(SOURCE_DIR, "src/backend/access/heap/heapam_indexscan.c")

TABLE = "ios_stress"
INDEX = "ios_stress_k_idx"

# Plain-index-scan scenario (the second variant).
TABLE_PLAIN = "pscan_stress"
INDEX_PLAIN = "pscan_stress_ab_idx"
MATCH_B = 1                 # predicate is WHERE b = MATCH_B; filler rows have b = 0
ITEMS_PER_LEAF = 360        # approx (a,b) index tuples per 8 KB leaf page (sizing only)

# Extensions needed for eviction/prewarm/layout verification.
EXTENSIONS = ["pg_prewarm", "pg_buffercache", "pg_visibility", "pageinspect"]


# ---------------------------------------------------------------------------
# Small helpers (cache control, SQL convenience)
# ---------------------------------------------------------------------------

def clear_os_cache():
    """Drop the OS page cache (sudo clear_cache.sh)."""
    r = subprocess.run(["sudo", "clear_cache.sh"], capture_output=True, check=False)
    if r.returncode != 0:
        print("  WARNING: failed to clear OS cache")


def q1(cur, sql, args=None):
    """Run a query, return the single scalar of the single row (or None)."""
    cur.execute(sql, args)
    row = cur.fetchone()
    return None if row is None else row[0]


def evict(cur, rels):
    """Evict relations from shared buffers (best-effort, with retries)."""
    for rel in rels:
        for attempt in range(4):
            try:
                cur.execute("SELECT buffers_skipped FROM "
                            "pg_buffercache_evict_relation(%s::regclass)", (rel,))
                skipped = cur.fetchone()[0]
                if skipped and attempt < 3:
                    time.sleep(0.05)
                    continue
            except psycopg.Error:
                pass
            break


def prewarm(cur, rel, vm=False, vm_only=False):
    """Prewarm a relation (and/or its visibility map fork) into shared buffers."""
    if not vm_only:
        cur.execute("SELECT pg_prewarm(%s)", (rel,))
    if vm or vm_only:
        cur.execute("SELECT pg_prewarm(%s, 'buffer', 'vm')", (rel,))


# ---------------------------------------------------------------------------
# Source constant cross-check (belt-and-suspenders; never authoritative)
# ---------------------------------------------------------------------------

def grep_source_constants():
    """Best-effort: read INDEX_PREFETCH_BLKSWITCH_THRESHOLD and
    INDEX_SCAN_MAX_BATCHES from the source tree.  Returns (T_src, maxbatches_src),
    either possibly None.  Used only to seed sweep ranges and sanity-check the
    empirically discovered values (and to flag a stale-binary-vs-source skew)."""
    import re
    t_src = mb_src = None
    try:
        with open(SRC_BLKSWITCH) as f:
            for line in f:
                m = re.search(r"#define\s+INDEX_PREFETCH_BLKSWITCH_THRESHOLD\s+(\d+)", line)
                if m:
                    t_src = int(m.group(1))
                    break
    except OSError:
        pass
    try:
        with open(SRC_MAX_BATCHES) as f:
            for line in f:
                m = re.search(r"#define\s+INDEX_SCAN_MAX_BATCHES\s+(\d+)", line)
                if m:
                    mb_src = int(m.group(1))
                    break
    except OSError:
        pass
    return t_src, mb_src


# ---------------------------------------------------------------------------
# Throwaway scratch cluster lifecycle
# ---------------------------------------------------------------------------

class Cluster:
    """A disposable PostgreSQL cluster started from a chosen build's binaries.

    Never touches the persistent data dirs (e.g. the 38 GB regression DB); the
    data dir lives under a scratch path and is removed on stop unless --keep."""

    def __init__(self, args):
        self.bindir = args.bindir
        self.datadir = args.datadir
        self.port = args.port
        self.io_method = args.io_method
        self.eic = args.effective_io_concurrency
        self.io_combine_limit = args.io_combine_limit
        self.keep = args.keep_cluster
        self.logfile = os.path.join(os.path.dirname(self.datadir.rstrip("/")),
                                    "postgres.log")
        self.dbname = "stress"

    def _bin(self, name):
        return os.path.join(self.bindir, name)

    def _safe_to_wipe(self):
        # Guard: only ever rm -rf a path that is clearly our scratch dir.
        d = os.path.abspath(self.datadir)
        return ("ios_overread" in d) or d.startswith("/tmp/")

    def init(self, rebuild):
        running = subprocess.run([self._bin("pg_ctl"), "status", "-D", self.datadir],
                                 capture_output=True, check=False).returncode == 0
        if running and not rebuild:
            return
        if running:
            self.stop()
        if rebuild and os.path.exists(self.datadir):
            if not self._safe_to_wipe():
                sys.exit(f"refusing to wipe non-scratch datadir {self.datadir}")
            shutil.rmtree(self.datadir)
        if not os.path.exists(os.path.join(self.datadir, "PG_VERSION")):
            os.makedirs(self.datadir, exist_ok=True)
            print(f"initdb {self.datadir} (build bin: {self.bindir})")
            r = subprocess.run(
                [self._bin("initdb"), "-D", self.datadir, "-U", "pg",
                 "--auth=trust", "--locale=C", "-E", "UTF8", "--no-sync"],
                capture_output=True, text=True, check=False)
            if r.returncode != 0:
                sys.exit(f"initdb failed:\n{r.stdout}\n{r.stderr}")

    def start(self):
        opts = [
            f"-k /tmp -p {self.port} -c listen_addresses=''",
            "-c shared_buffers=3GB",
            "-c autovacuum=off",
            "-c maintenance_work_mem=1GB",
            "-c max_parallel_workers_per_gather=0",
            "-c track_io_timing=on",
            f"-c effective_io_concurrency={self.eic}",
            f"-c io_combine_limit={self.io_combine_limit}",
            f"-c io_method={self.io_method}",
        ]
        cmd = [self._bin("pg_ctl"), "start", "-D", self.datadir, "-l", self.logfile,
               "-w", "-t", "60"]
        for o in opts:
            cmd += ["-o", o]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            tail = ""
            try:
                with open(self.logfile) as f:
                    tail = "".join(f.readlines()[-25:])
            except OSError:
                pass
            sys.exit(f"pg_ctl start failed:\n{r.stdout}\n{r.stderr}\n--- log ---\n{tail}")
        # Create the scratch DB + extensions.
        admin = self.connect(dbname="postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            exists = q1(cur, "SELECT 1 FROM pg_database WHERE datname=%s", (self.dbname,))
            if not exists:
                cur.execute(f"CREATE DATABASE {self.dbname}")
        admin.close()
        conn = self.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            for ext in EXTENSIONS:
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        conn.close()

    def connect(self, dbname=None):
        return psycopg.connect(host="/tmp", port=self.port, user="pg",
                               dbname=dbname or self.dbname, connect_timeout=10)

    def alive(self):
        try:
            c = self.connect()
            c.close()
            return True
        except psycopg.Error:
            return False

    def log_size(self):
        try:
            return os.path.getsize(self.logfile)
        except OSError:
            return 0

    def log_since(self, offset):
        try:
            with open(self.logfile) as f:
                f.seek(offset)
                return f.read()
        except OSError:
            return ""

    def stop(self):
        subprocess.run([self._bin("pg_ctl"), "stop", "-D", self.datadir, "-m", "fast"],
                       capture_output=True, check=False)

    def cleanup(self):
        self.stop()
        if not self.keep and self._safe_to_wipe() and os.path.exists(self.datadir):
            shutil.rmtree(self.datadir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Layout build + verification
# ---------------------------------------------------------------------------

def build_layout(conn, num_dirty, n_tail, pad_bytes, fillfactor):
    """Build the adversarial layout (see module docstring).  Returns measured
    facts: dict with d_switch (non-all-visible prefix pages), idx_leaf_pages,
    correlation."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(
            f"CREATE UNLOGGED TABLE {TABLE} (k bigint NOT NULL, pad bytea NOT NULL) "
            f"WITH (fillfactor = {fillfactor}, autovacuum_enabled = off)")
        # STORAGE PLAIN: keep the wide dirty pad inline (no TOAST), so each wide
        # dirty row really occupies (about) its own heap page -> ~1 row/page.
        cur.execute(f"ALTER TABLE {TABLE} ALTER COLUMN pad SET STORAGE PLAIN")

        # Dirty prefix: num_dirty rows, all k=0, each ~one per heap page.
        cur.execute(
            f"INSERT INTO {TABLE} (k, pad) "
            f"SELECT 0, repeat('x', {pad_bytes})::bytea "
            f"FROM generate_series(1, {num_dirty})")
        # All-visible tail: unique increasing keys, tiny pad (many per page).
        cur.execute(
            f"INSERT INTO {TABLE} (k, pad) "
            f"SELECT g, ''::bytea FROM generate_series(1, {n_tail}) g")

        # Build the covering index with deduplication ON so k=0 becomes a posting
        # list (the unkillability trick depends on this).
        cur.execute(
            f"CREATE INDEX {INDEX} ON {TABLE} (k) WITH (deduplicate_items = on)")

        # Make EVERYTHING all-visible/frozen first.
        cur.execute(f"VACUUM (FREEZE, ANALYZE) {TABLE}")

        # Delete all k=0 rows except the highest-ctid anchor.  The anchor keeps
        # the k=0 posting list unkillable; the rest become heap LP_DEAD stubs.
        anchor = q1(cur, f"SELECT ctid FROM {TABLE} WHERE k=0 ORDER BY ctid DESC LIMIT 1")
        cur.execute(f"DELETE FROM {TABLE} WHERE k=0 AND ctid <> %s", (anchor,))

        # Prune deleted tuples to LP_DEAD stubs WITHOUT touching the index
        # (INDEX_CLEANUP off keeps the posting-list TIDs in place) and without
        # re-setting VM bits on the dirty pages.
        cur.execute(f"VACUUM (INDEX_CLEANUP off, TRUNCATE off) {TABLE}")
        cur.execute("CHECKPOINT")

        facts = measure_layout(cur)
    return facts


def measure_layout(cur):
    """Read back the layout facts used by verify_layout / reporting."""
    d_switch = q1(cur,
                  f"SELECT count(*) FROM pg_visibility(%s::regclass) "
                  f"WHERE NOT all_visible", (TABLE,))
    idx_leaf = q1(cur, f"SELECT pg_relation_size(%s::regclass) / 8192", (INDEX,))
    corr = q1(cur, "SELECT correlation FROM pg_stats "
                   "WHERE tablename=%s AND attname='k'", (TABLE,))
    return {"d_switch": d_switch, "idx_leaf_pages": idx_leaf, "correlation": corr}


def verify_layout(cur, facts, want_backstop):
    """Assert the layout actually produces the pathology preconditions.  Raises
    AssertionError with a diagnostic rather than silently measuring a bad
    layout."""
    d = facts["d_switch"]
    # (a) The non-all-visible pages must be a contiguous prefix [0, d).
    bad = q1(cur, f"SELECT count(*) FROM pg_visibility(%s::regclass) "
                  f"WHERE NOT all_visible AND blkno >= %s", (TABLE, d))
    assert bad == 0, f"non-all-visible pages are not a clean prefix ({bad} stragglers)"
    after = q1(cur, f"SELECT count(*) FROM pg_visibility(%s::regclass) "
                    f"WHERE blkno >= %s AND NOT all_visible", (TABLE, d))
    assert after == 0, f"{after} pages in the tail are not all-visible"
    # (b) The prefix pages carry heap LP_DEAD stubs (lp_flags=3).
    if d > 0:
        dead = q1(cur,
                  "SELECT count(*) FROM generate_series(0, %s) b, "
                  "LATERAL heap_page_items(get_raw_page(%s,'main',b)) "
                  "WHERE lp_flags = 3", (d - 1, TABLE))
        assert dead and dead > 0, "no LP_DEAD heap stubs in the dirty prefix"
    # (c) The k=0 index tuple is an UNKILLABLE posting list: btkillitemsbatch
    #     cannot LP_DEAD-mark it because the live anchor TID is never reported
    #     dead.  Its proof is the repeatability re-run (the over-read must persist
    #     across scans); nothing extra to assert structurally here.
    # (d) Enough tail leaf pages to saturate the backstop.
    assert facts["idx_leaf_pages"] > want_backstop + 2, (
        f"index has only {facts['idx_leaf_pages']} leaf pages; need >> "
        f"{want_backstop} to saturate the backstop -- raise --n-tail")
    # (e) Scan order tracks heap order so a tiny LIMIT needs ~1 leaf page.
    corr = facts["correlation"]
    assert corr is None or corr > 0.99, f"correlation {corr} too low (want ~1)"


def probe_items_per_leaf(conn):
    """Measure the actual (a,b)-index live items per leaf page (pageinspect
    bt_page_stats) so RUN_STRIDE places ~1 match per leaf page at gap=0 -- rather
    than assuming a fixed value that would smear matches across leaf pages."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _ipl_probe")
        cur.execute("CREATE UNLOGGED TABLE _ipl_probe (a bigint, b int)")
        cur.execute("INSERT INTO _ipl_probe SELECT g, 0 FROM generate_series(1, 30000) g")
        cur.execute("CREATE INDEX _ipl_probe_idx ON _ipl_probe (a, b) WITH (fillfactor = 90)")
        ipl = q1(cur,
            "SELECT avg(s.live_items)::int "
            "FROM generate_series(1, (pg_relation_size('_ipl_probe_idx')::int / 8192) - 1) g, "
            "     LATERAL bt_page_stats('_ipl_probe_idx', g) s WHERE s.type = 'l'")
        cur.execute("DROP TABLE _ipl_probe")
    return ipl or ITEMS_PER_LEAF


def build_plain(conn, args, arming=None, region=None):
    """Build the PLAIN-index-scan adversarial layout (see module docstring).

    Composite index (a, b); `a` UNIQUE ⇒ a full Index Scan with `b=MATCH_B` as a
    per-item Index Cond, NOT a skip scan.  `n_arm` WIDE arming rows (pad ⇒ ~1
    row/heap page) with the smallest `a` share the first leaf page but sit on
    distinct heap blocks ⇒ arm prefetch while needed≈1 leaf page.  A long
    contiguous run of narrow b=MATCH_B rows with `a` spread by RUN_STRIDE clusters
    onto ONE heap block yet spreads across leaf pages ⇒ same-block over-read.
    Narrow b=0 filler in between ⇒ ~1 match/leaf page + the no-match leaf pages the
    gap knob (G) controls (RUN_STRIDE = items_per_leaf × (G+1))."""
    n_arm = arming if arming is not None else args.plain_arming
    runlen = region if region is not None else args.plain_runlen
    gap = args.plain_nomatch_gap
    ipl = probe_items_per_leaf(conn)
    stride = ipl * (gap + 1)
    run_start = max(ipl * 2, n_arm + ipl)
    run_end = run_start + stride * (runlen - 1)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE_PLAIN}")
        cur.execute(f"CREATE UNLOGGED TABLE {TABLE_PLAIN} "
                    f"(a bigint NOT NULL, b int NOT NULL, pad bytea NOT NULL) "
                    f"WITH (fillfactor = 90, autovacuum_enabled = off)")
        cur.execute(f"ALTER TABLE {TABLE_PLAIN} ALTER COLUMN pad SET STORAGE PLAIN")
        # Arming: n_arm WIDE rows (~1 row/heap page), b=MATCH_B, smallest a.
        cur.execute(f"INSERT INTO {TABLE_PLAIN} (a, b, pad) "
                    f"SELECT g, {MATCH_B}, repeat('x', {args.plain_arm_pad})::bytea "
                    f"FROM generate_series(1, {n_arm}) g")
        # Filler: narrow rows, b=0, a in (n_arm, run_end] EXCLUDING the run a-values.
        cur.execute(f"INSERT INTO {TABLE_PLAIN} (a, b, pad) "
                    f"SELECT g, 0, ''::bytea FROM generate_series({n_arm} + 1, {run_end}) g "
                    f"WHERE g < {run_start} OR (g - {run_start}) % {stride} <> 0")
        # Run: narrow rows, b=MATCH_B, a spread by stride, inserted contiguously ⇒
        # clustered onto ~one heap block; each lands on a distinct leaf page.
        cur.execute(f"INSERT INTO {TABLE_PLAIN} (a, b, pad) "
                    f"SELECT g, {MATCH_B}, ''::bytea "
                    f"FROM generate_series({run_start}, {run_end}, {stride}) g")
        cur.execute(f"CREATE INDEX {INDEX_PLAIN} ON {TABLE_PLAIN} (a, b)")
        cur.execute(f"VACUUM (ANALYZE) {TABLE_PLAIN}")
        cur.execute("CHECKPOINT")

        idx_leaf = q1(cur, "SELECT pg_relation_size(%s::regclass) / 8192", (INDEX_PLAIN,))
        arming_blocks = q1(cur,
            f"SELECT count(DISTINCT (ctid::text::point)[0]) "
            f"FROM {TABLE_PLAIN} WHERE b = {MATCH_B} AND a <= {n_arm}")
        # Heap side: longest scan(a)-order run of matches on a single heap block.
        run_max = q1(cur,
            "SELECT coalesce(max(cnt), 0) FROM ("
            "  SELECT count(*) AS cnt FROM ("
            "    SELECT (ctid::text::point)[0] AS blk, "
            "           row_number() OVER (ORDER BY a) "
            "           - row_number() OVER (PARTITION BY (ctid::text::point)[0] ORDER BY a) AS grp "
            f"    FROM {TABLE_PLAIN} WHERE b = {MATCH_B} AND a >= {run_start}"
            "  ) s GROUP BY blk, grp) t")
        # The dominant run heap block (where the long same-block run lives).
        p0 = q1(cur, f"SELECT (ctid::text::point)[0]::int FROM {TABLE_PLAIN} "
                     f"WHERE b = {MATCH_B} AND a >= {run_start} "
                     f"GROUP BY (ctid::text::point)[0] ORDER BY count(*) DESC LIMIT 1")
        # Index side (pageinspect): over every LEAF page, count entries whose heap
        # TID points to the run block p0.  Confirms the run's index entries are
        # spread ~1 per leaf page (so each prefetched batch is a distinct leaf
        # page) and really point to p0 -- i.e. key+TID layout is as expected.
        cur.execute(
            "WITH leaves AS ("
            f"  SELECT g AS blkno FROM generate_series(1, "
            f"     (pg_relation_size('{INDEX_PLAIN}')::int / 8192) - 1) g "
            f"  WHERE (bt_page_stats('{INDEX_PLAIN}', g)).type = 'l') "
            "SELECT count(*) FILTER (WHERE c > 0), coalesce(max(c), 0) FROM ("
            "  SELECT l.blkno, count(*) FILTER (WHERE (i.ctid::text::point)[0] = %s) AS c "
            f"  FROM leaves l, LATERAL bt_page_items('{INDEX_PLAIN}', l.blkno) i "
            "  GROUP BY l.blkno) s", (p0,))
        run_leaf_pages, max_match_per_leaf = cur.fetchone()
    return {"idx_leaf_pages": idx_leaf, "arming": arming_blocks,
            "arming_blocks": arming_blocks, "run_max_same_block": run_max,
            "items_per_leaf": ipl, "p0_block": p0, "run_leaf_pages": run_leaf_pages,
            "max_match_per_leaf": max_match_per_leaf,
            "n_arm": n_arm, "runlen": runlen, "gap": gap, "stride": stride,
            "correlation": None}


def verify_layout_plain(cur, facts, want_backstop):
    """Assert the plain-scan layout produces the pathology preconditions, including
    a pageinspect check that the index leaf layout (key + heap TID) is as
    expected: each run-region leaf page carries ~1 entry pointing to the run heap
    block."""
    assert facts["arming_blocks"] == facts["n_arm"], (
        f"arming rows not on distinct heap blocks ({facts['arming_blocks']} blocks "
        f"for {facts['n_arm']} rows) -- raise --plain-arm-pad")
    assert facts["arming_blocks"] >= 4, (
        f"only {facts['arming_blocks']} arming blocks; need >= the arming threshold")
    # Heap: the run is one long same-block stretch (drives the same-block continue).
    assert facts["run_max_same_block"] > want_backstop, (
        f"longest same-heap-block run is {facts['run_max_same_block']}; need > "
        f"{want_backstop} to saturate the over-read -- raise --plain-runlen")
    # Index (pageinspect): run matches spread ~1 per leaf page (so each prefetched
    # batch is a fresh leaf page), each pointing to the run heap block p0.
    assert facts["max_match_per_leaf"] <= 2, (
        f"run matches are not ~1 per leaf page (max {facts['max_match_per_leaf']}/leaf) "
        f"-- index leaf layout off (items_per_leaf={facts['items_per_leaf']})")
    assert facts["run_leaf_pages"] > want_backstop, (
        f"only {facts['run_leaf_pages']} leaf pages carry a run match; need > "
        f"{want_backstop} to saturate the over-read -- raise --plain-runlen")
    assert facts["idx_leaf_pages"] > want_backstop + 2, (
        f"index has only {facts['idx_leaf_pages']} leaf pages; need >> {want_backstop}")


# ---------------------------------------------------------------------------
# Plan forcing + measurement
# ---------------------------------------------------------------------------

# Force an Index Only Scan (covering query + visibility-driven heap skips).
FORCE_IOS_GUCS = {
    "enable_seqscan": "off",
    "enable_bitmapscan": "off",
    "enable_indexscan": "off",
    "enable_indexonlyscan": "on",
    "max_parallel_workers_per_gather": "0",
}

# Force a plain Index Scan: enable_indexscan ON, indexonlyscan OFF (and the query
# selects a non-indexed column) so the heap is fetched -> prefetch arms.
FORCE_PLAIN_GUCS = {
    "enable_seqscan": "off",
    "enable_bitmapscan": "off",
    "enable_indexscan": "on",
    "enable_indexonlyscan": "off",
    "max_parallel_workers_per_gather": "0",
}


def disable_prefetch_guc_value(prefetch):
    """Map a prefetch on/off setting to the value to use for the
    debug_disable_indexscan_prefetch GUC, whose meaning is inverted (on means
    that prefetching is disabled)."""
    return "off" if str(prefetch).lower() in ("on", "true", "1") else "on"


def set_session(cur, prefetch, force_gucs, eic=None, combine=None):
    for g, v in force_gucs.items():
        cur.execute(f"SET {g} = {v}")
    if prefetch is not None:  # None => build lacks the GUC (e.g. master)
        cur.execute("SET debug_disable_indexscan_prefetch = "
                    f"{disable_prefetch_guc_value(prefetch)}")
    if eic is not None:
        cur.execute(f"SET effective_io_concurrency = {eic}")
    if combine is not None:
        cur.execute(f"SET io_combine_limit = {combine}")


def _walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


def find_node(plan_json, node_type):
    for n in _walk(plan_json["Plan"]):
        if n.get("Node Type") == node_type:
            return n
    return None


def assert_plan(cur, sql, index, node_type):
    cur.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
    plan = cur.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    node = find_node(plan[0], node_type)
    if node is None or node.get("Index Name") != index:
        raise AssertionError(f"plan is not a '{node_type}' on {index}: {plan}")


def idx_blks(cur, index):
    """Index-scoped buffer accesses (read+hit) from cumulative stats.

    Force a flush of this backend's pending stats so the per-query delta is
    accurate (pgstat is otherwise rate-limited to ~1 Hz)."""
    try:
        cur.execute("SELECT pg_stat_force_next_flush()")
    except psycopg.Error:
        pass
    cur.execute("SELECT pg_stat_clear_snapshot()")
    return q1(cur,
              "SELECT coalesce(idx_blks_read,0) + coalesce(idx_blks_hit,0) "
              "FROM pg_statio_user_indexes WHERE indexrelname=%s", (index,))


def run_once(cur, limit, prefetch, sc, eic, combine):
    """Run the scenario's query once under EXPLAIN (ANALYZE, BUFFERS); return a
    dict with exec_ms, node buffers, heap fetches, actual rows, and the
    index-scoped idx_blks delta.  (For a plain Index Scan 'Heap Fetches' is not
    reported -> 0; the index-scoped idx_blks delta still isolates the leaf-page
    over-read, since heap buffers are identical on/off.)"""
    set_session(cur, prefetch, sc.force_gucs, eic, combine)
    before = idx_blks(cur, sc.index)
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, FORMAT JSON) "
                + sc.query(limit))
    out = cur.fetchone()[0]
    if isinstance(out, str):
        out = json.loads(out)
    after = idx_blks(cur, sc.index)
    root = out[0]
    node = find_node(root, sc.node_type)
    nbuf = (node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0))
    return {
        "exec_ms": root.get("Execution Time"),
        "node_buffers": nbuf,
        "heap_fetches": node.get("Heap Fetches", 0),
        "actual_rows": node.get("Actual Rows", 0),
        "idx_blks_delta": (after - before) if (after is not None and before is not None)
                          else None,
    }


def parse_explain(lines):
    """Pull the headline numbers out of EXPLAIN (ANALYZE, BUFFERS) text: total
    buffers (top node), shared read I/O time, heap fetches, and execution time."""
    import re
    buf = hf = exe = iordt = None
    for ln in lines:
        # The top node's "Buffers: shared ..." line.  hit= and read= are each
        # optional (a fully-uncached read shows "shared read=N" with no hit=, a
        # fully-cached one shows "shared hit=N" with no read=), so parse them
        # independently and sum.
        if buf is None and "Buffers: shared" in ln:
            h = re.search(r"\bhit=(\d+)", ln)
            rd = re.search(r"\bread=(\d+)", ln)
            if h or rd:
                buf = (int(h.group(1)) if h else 0) + (int(rd.group(1)) if rd else 0)
        if iordt is None and "I/O Timings" in ln:
            m = re.search(r"read=([\d.]+)", ln)
            if m:
                iordt = float(m.group(1))
        m = re.search(r"Heap Fetches: (\d+)", ln)
        if m:
            hf = int(m.group(1))
        m = re.search(r"Execution Time: ([\d.]+) ms", ln)
        if m:
            exe = float(m.group(1))
    return {"buffers": buf, "heap_fetches": hf, "exec_ms": exe, "io_read_ms": iordt}


class Scenario:
    """One adversarial scan variant: the IoS all-visible over-read, or the plain
    same-heap-block over-read.  Bundles the table/index, plan forcing, query, the
    layout builder/verifier, and a one-line description of what the over-read is."""

    def __init__(self, name, table, index, node_type, force_gucs,
                 query, limit, build, verify, stmt, note):
        self.name = name
        self.table = table
        self.index = index
        self.node_type = node_type
        self.force_gucs = force_gucs
        self.query = query        # query(limit) -> sql
        self.limit = limit        # demo LIMIT (>= the arming threshold for plain)
        self.build = build        # build(conn) -> facts
        self.verify = verify      # verify(cur, facts, want_backstop)
        self.stmt = stmt          # prepared-statement name
        self.note = note          # what the over-read is, for the report


def ios_scenario(args):
    return Scenario(
        "ios", TABLE, INDEX, "Index Only Scan", FORCE_IOS_GUCS,
        query=lambda lim: f"SELECT k FROM {TABLE} ORDER BY k LIMIT {lim}",
        limit=args.calib_limit,
        build=lambda conn: build_layout(conn, args.num_dirty or 6, args.n_tail,
                                        args.pad_bytes, args.fillfactor),
        verify=lambda cur, facts, wb: verify_layout(cur, facts, wb),
        stmt="ios_q",
        note="the index-only-scan all-visible over-read (bounded by the 64-batch ring)")


def plain_scenario(args):
    return Scenario(
        "plain", TABLE_PLAIN, INDEX_PLAIN, "Index Scan", FORCE_PLAIN_GUCS,
        query=lambda lim: (f"SELECT a, b, pad FROM {TABLE_PLAIN} "
                           f"WHERE b = {MATCH_B} ORDER BY a LIMIT {lim}"),
        limit=args.plain_limit,
        build=lambda conn: build_plain(conn, args),
        verify=lambda cur, facts, wb: verify_layout_plain(cur, facts, wb),
        stmt="plain_q",
        note="the plain-scan same-heap-block over-read")


def select_scenarios(args):
    table = {"ios": ios_scenario(args), "plain": plain_scenario(args)}
    return ([table["ios"], table["plain"]] if args.variant == "both"
            else [table[args.variant]])


def _explain_one_build(label, bindir, port, args, modes, sc):
    """Spin up a throwaway cluster from `bindir`, build the scenario's layout, and
    return EXPLAIN (ANALYZE, BUFFERS) blocks for each (cache mode x prefetch
    variant).

    Uses a PREPAREd generic plan (plan_cache_mode=force_generic_plan) so the
    EXPLAIN ANALYZE reflects no planning work -- planning noise is removed.
    (io_read timing only shows when there are real reads, i.e. uncached -- that's
    just how Postgres reports I/O; nothing here forces a cache state.)"""
    import copy
    a = copy.copy(args)
    a.bindir = bindir
    a.datadir = f"/tmp/ios_overread_{label}_{sc.name}/pgdata"
    a.port = port
    cl = Cluster(a)
    blocks = []
    try:
        cl.init(rebuild=not args.reuse_cluster)
        cl.start()
        conn = cl.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            has_guc = q1(cur, "SELECT 1 FROM pg_settings "
                              "WHERE name='debug_disable_indexscan_prefetch'")
        facts = sc.build(conn)
        with conn.cursor() as cur:
            sc.verify(cur, facts, args.assumed_backstop)
        variants = ([("off", "off"), ("on", "on")] if has_guc
                    else [(None, None)])
        with conn.cursor() as cur:
            # Force the scenario's plan, then PREPARE a generic plan and warm it so
            # the EXPLAIN ANALYZE below carries no planning work.
            # debug_disable_indexscan_prefetch is an execution-time switch, so the same
            # plan serves both off and on.
            for g, v in sc.force_gucs.items():
                cur.execute(f"SET {g} = {v}")
            cur.execute("SET plan_cache_mode = force_generic_plan")
            cur.execute(f"PREPARE {sc.stmt} AS {sc.query(sc.limit)}")
            cur.execute(f"EXECUTE {sc.stmt}")          # lock in the generic plan
            cur.fetchall()
            cur.execute(f"EXPLAIN (COSTS OFF) EXECUTE {sc.stmt}")
            ptxt = [r[0] for r in cur.fetchall()]
            if not any(sc.node_type in ln for ln in ptxt):
                raise AssertionError(f"{label}/{sc.name}: prepared plan is not a "
                                     f"'{sc.node_type}':\n" + "\n".join(ptxt))
            for mode in modes:
                for vlabel, pref in variants:
                    prepare_cache(cur, mode, sc)
                    if pref is not None:
                        cur.execute("SET debug_disable_indexscan_prefetch = "
                                    f"{disable_prefetch_guc_value(pref)}")
                    # TIMING OFF: suppress per-node wall-clock timing, but keep
                    # the I/O Timings (track_io_timing) and total Execution Time.
                    cur.execute("EXPLAIN (ANALYZE, WAL, VERBOSE, BUFFERS, TIMING OFF) "
                                f"EXECUTE {sc.stmt}")
                    text = [r[0] for r in cur.fetchall()]
                    blocks.append({"build": label, "variant": vlabel, "mode": mode,
                                   "text": text, "p": parse_explain(text)})
        conn.close()
    finally:
        cl.cleanup()
    return blocks


def _block_name(b):
    v = b["variant"]
    return (f"{b['build']} prefetch={v}" if v in ("off", "on")
            else f"{b['build']} (no prefetch)")


def _cell(x, w, dec=None):  # None-safe (plain Index Scan has no Heap Fetches)
    if x is None:
        return f"{'-':>{w}}"
    return f"{x:>{w}.{dec}f}" if dec is not None else f"{x:>{w}}"


def explain_gather(args, sc):
    """Bring up master + patch clusters, build the scenario's layout, and return
    (blocks, modes) -- the EXPLAIN blocks for each (cache mode x prefetch variant).
    No printing, so the caller can show ALL plans before ALL summaries."""
    modes = (["cached", "uncached"] if args.cache_mode == "both"
             else [args.cache_mode])
    blocks = _explain_one_build("master", BUILD_BIN["master"], args.port + 2, args, modes, sc)
    if args.build != "master":
        blocks += _explain_one_build(args.build, args.bindir, args.port, args, modes, sc)
    return blocks, modes


def explain_print_plans(sc, blocks, modes):
    """Print the full EXPLAIN plans for one scenario, grouped by behavior (cache
    mode) then by branch/config (master, prefetch=off, prefetch=on)."""
    print(f"\n\n##################################################################")
    print(f"############   VARIANT: {sc.name}   ({sc.node_type})")
    print(f"##################################################################")
    for mode in modes:
        print(f"\n################  {sc.name}  /  {mode}  ################")
        for b in [x for x in blocks if x["mode"] == mode]:
            print(f"\n----- {_block_name(b)}  [{sc.name}/{mode}] -----")
            for ln in b["text"]:
                print("  " + ln)


def explain_print_summary(args, sc, blocks, modes):
    """Print the prominent summary tables for one scenario.  'exec x' is the
    exec-time ratio (this/master); > 1.00x = regression (same convention as
    prefetch_benchmark.py).  'vs master' is the buffer delta (the over-read)."""
    bar = "=" * 96
    print("\n\n" + bar)
    print(f"SUMMARY [{sc.name}]  --  master vs patch:  {sc.query(sc.limit)}")
    print(f"             forced {sc.node_type}, prepared generic plan (no planning noise)")
    print("             track_io_timing=on; io_read shown only when there are real reads")
    print("             'buffers (h+r)' = shared buffers accessed, counting hits AND reads")
    print("             indifferently; 'bufs vs master' = buffer delta (the over-read);")
    print("             'exec x' = exec-time ratio this/master (> 1.00x = regression)")
    print(bar)
    for mode in modes:
        mb = [b for b in blocks if b["mode"] == mode]
        base = next((b for b in mb if b["build"] == "master"), None)
        base_exec = base["p"]["exec_ms"] if base else None
        base_buf = base["p"]["buffers"] if base else None
        print(f"\n  === {sc.name} / {mode} ===")
        print(f"  {'build / setting':<24}{'buffers (h+r)':>14}{'bufs vs master':>16}"
              f"{'heap_fetch':>11}{'io_read(ms)':>12}{'exec(ms)':>10}{'exec x':>8}")
        print("  " + "-" * 94)
        for b in mb:
            p = b["p"]
            ratio = (f"{p['exec_ms'] / base_exec:.2f}x"
                     if (base_exec and p["exec_ms"] is not None) else "-")
            delta = "-"
            if base_buf is not None and p["buffers"] is not None:
                d = p["buffers"] - base_buf
                delta = (f"+{d}" if d > 0 else "identical" if d == 0 else f"{d}")
            print(f"  {_block_name(b):<24}{_cell(p['buffers'],14)}{delta:>16}"
                  f"{_cell(p['heap_fetches'],11)}{_cell(p['io_read_ms'],12,3)}"
                  f"{_cell(p['exec_ms'],10,3)}{ratio:>8}")
    print(f"\n  => identical plan + result; patch prefetch=on alone reads the extra leaf")
    print(f"     buffers -- {sc.note}.")
    if sc.name == "plain":
        print("     NOTE: when the read stream asks for the next block to prefetch, the")
        print("     callback churns through same-heap-block batches (returning nothing)")
        print("     until it finds a different block -- bounded only by INDEX_SCAN_MAX_BATCHES")
        print("     and by how many index entries point at one heap block (max items/heap")
        print("     page).  Prefetch distance is irrelevant.  Unlike IoS (one callback call")
        print("     churns to ring-full at LIMIT 1), the plain case is FAR more parameter-")
        print("     sensitive: the run-churn must line up to begin around when the scan has")
        print("     already produced its LIMIT rows, so the demo LIMIT is tuned to that.")
        if args.plain_nomatch_gap > 0:
            print("     With --plain-nomatch-gap > 0 the buffer over-read grows PAST the ring,")
            print("     because _bt_readnextpage reads no-match leaf pages that are never")
            print("     batched -- partly INHERENT to any index read-ahead (under a LIMIT you")
            print("     can always make reading one more tuple ahead cost more), an accepted")
            print("     risk, NOT a distinct bug.  Hence the knob is off by default.")
    print(bar)


def verify_repeatable(conn, limit, sc, n=4):
    """Prove the posting-list trick holds (IoS): run the armed scan n times on the
    SAME layout and confirm heap fetches and the over-read stay constant.  If
    btkillitemsbatch could kill the arming index tuples, later scans would skip
    them -> fewer heap fetches / no arming / collapsed over-read (DRIFT)."""
    conn.autocommit = True
    hfs, bufs = [], []
    with conn.cursor() as cur:
        for _ in range(n):
            prepare_cache(cur, "cached", sc)
            r = run_once(cur, limit, "on", sc, None, None)
            hfs.append(r["heap_fetches"])
            bufs.append(r["node_buffers"])
    stable = len(set(hfs)) == 1 and len(set(bufs)) == 1
    verdict = ("STABLE (posting-list trick holds; arming tuples never killed)"
               if stable else "DRIFT -- arming index tuples were killed!")
    print(f"\n== Repeatability ({n} scans, same layout) ==\n"
          f"   heap_fetches={hfs} node_buffers={bufs}  ->  {verdict}")
    return stable


def prepare_cache(cur, cache_mode, sc):
    if cache_mode == "cached":
        prewarm(cur, sc.index)
        prewarm(cur, sc.table, vm=True)
    else:  # uncached
        evict(cur, [sc.index, sc.table])
        prewarm(cur, sc.table, vm_only=True)
        clear_os_cache()


def measure_config(conn, limit, cache_mode, runs, sc, eic=None, combine=None):
    """Measure extra_leaf_reads (on - off) and timing for one config, resetting
    the cache state identically before each on/off run.  Returns a metrics dict.
    Asserts Heap Fetches equal on/off (the isolation precondition)."""
    conn.autocommit = True
    with conn.cursor() as cur:
        assert_plan(cur, sc.query(limit), sc.index, sc.node_type)
        on_runs, off_runs = [], []
        for _ in range(runs):
            prepare_cache(cur, cache_mode, sc)
            off_runs.append(run_once(cur, limit, "off", sc, eic, combine))
            prepare_cache(cur, cache_mode, sc)
            on_runs.append(run_once(cur, limit, "on", sc, eic, combine))

    def med(rs, key):
        vals = [r[key] for r in rs if r[key] is not None]
        return statistics.median(vals) if vals else None

    off_nbuf = med(off_runs, "node_buffers")
    on_nbuf = med(on_runs, "node_buffers")
    off_idx = med(off_runs, "idx_blks_delta")
    on_idx = med(on_runs, "idx_blks_delta")
    hf_off = med(off_runs, "heap_fetches")
    hf_on = med(on_runs, "heap_fetches")
    # Isolation precondition: heap fetches must match (so the buffer delta is
    # pure index-leaf over-read).
    heap_equal = (hf_off == hf_on)

    extra_node = (on_nbuf - off_nbuf) if (on_nbuf is not None and off_nbuf is not None) else None
    extra_idx = (on_idx - off_idx) if (on_idx is not None and off_idx is not None) else None
    needed = off_idx
    waste = (extra_idx / needed) if (extra_idx is not None and needed) else None
    t_off = med(off_runs, "exec_ms")
    t_on = med(on_runs, "exec_ms")
    return {
        "limit": limit, "cache": cache_mode, "eic": eic, "combine": combine,
        "extra_leaf_reads": extra_idx,           # index-scoped (primary)
        "extra_node_buffers": extra_node,        # combined-node cross-check
        "needed_leaf_pages": needed,
        "waste_ratio": waste,
        "heap_fetches": hf_off, "heap_equal": heap_equal,
        "actual_rows": med(off_runs, "actual_rows"),
        "t_off_ms": t_off, "t_on_ms": t_on,
        "time_ratio": (t_on / t_off) if (t_on and t_off) else None,
        "time_abs_ms": (t_on - t_off) if (t_on is not None and t_off is not None) else None,
    }


# ---------------------------------------------------------------------------
# Self-calibration: discover T (arming threshold) and B (effective backstop)
# ---------------------------------------------------------------------------

def calibrate_T(cluster, args, sc):
    """Discover the arming threshold T = smallest D_switch that produces a
    non-zero over-read.  Rebuilds the prefix at increasing sizes.  Returns
    (T, list of (d_switch, extra_leaf_reads))."""
    print("\n== Calibrating arming threshold T (sweeping dirty-prefix size) ==")
    trace = []
    found = None
    # D_switch = num_dirty - 1; sweep num_dirty from 1 upward.
    cap = args.t_cap
    for num_dirty in range(1, cap + 2):
        conn = cluster.connect()
        facts = build_layout(conn, num_dirty, args.n_tail, args.pad_bytes, args.fillfactor)
        with conn.cursor() as cur:
            verify_layout(cur, facts, args.assumed_backstop)
        m = measure_config(conn, args.calib_limit, "cached", 1, sc)
        conn.close()
        d = facts["d_switch"]
        extra = m["extra_leaf_reads"]
        trace.append((d, extra))
        print(f"   num_dirty={num_dirty:<3} D_switch={d:<3} extra_leaf_reads={extra}")
        if extra and extra > args.arm_min and found is None:
            found = d
            # one more past the threshold is enough to confirm the jump
            break
    return found, trace


def calibrate_B(cluster, args, num_dirty, sc):
    """Discover the effective backstop B = the saturation plateau of
    extra_leaf_reads as the tail grows.  Returns (B, list of (idx_leaf, extra))."""
    print("\n== Calibrating effective backstop B (growing the all-visible tail) ==")
    trace = []
    best = 0
    last = None
    for n_tail in args.b_tail_steps:
        conn = cluster.connect()
        facts = build_layout(conn, num_dirty, n_tail, args.pad_bytes, args.fillfactor)
        with conn.cursor() as cur:
            # don't enforce the backstop precondition here; we are measuring it
            verify_layout(cur, facts, 0)
        m = measure_config(conn, args.calib_limit, "cached", 1, sc)
        conn.close()
        extra = m["extra_leaf_reads"]
        trace.append((facts["idx_leaf_pages"], extra))
        print(f"   n_tail={n_tail:<8} idx_leaf={facts['idx_leaf_pages']:<6} "
              f"extra_leaf_reads={extra}")
        if extra is not None:
            best = max(best, extra)
            # plateau: stops growing once tail exceeds the ring
            if last is not None and extra <= last:
                break
            last = extra
    return best, trace


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_row(m):
    def f(x, w=8, p=2):
        return ("{:>%d.%df}" % (w, p)).format(x) if isinstance(x, float) else f"{str(x):>{w}}"
    print(f"   LIMIT={f(m['limit'],4,0)} cache={m['cache']:<8} "
          f"eic={f(m['eic'],4,0)} comb={f(m['combine'],4,0)} | "
          f"extra_leaf={f(m['extra_leaf_reads'],5,0)} needed={f(m['needed_leaf_pages'],4,0)} "
          f"waste={f(m['waste_ratio'],6,1)}x | "
          f"hf={f(m['heap_fetches'],3,0)}{'' if m['heap_equal'] else '!MISMATCH'} "
          f"t_off={f(m['t_off_ms'],7)} t_on={f(m['t_on_ms'],7)} "
          f"({f(m['time_ratio'],5,2)}x)")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", choices=["rc", "dc", "master"], default="rc",
                   help="rc=patch release (timing), dc=patch cassert (assert hunt), "
                        "master=master release (no prefetch; for --explain comparison)")
    p.add_argument("--explain", action=argparse.BooleanOptionalAction, default=True,
                   help="[default] print the master-vs-patch EXPLAIN (ANALYZE, BUFFERS) "
                        "comparison and exit; pass --no-explain to run the full "
                        "calibration/sweep instead")
    p.add_argument("--variant", choices=["ios", "plain", "both"], default="both",
                   help="scan variant(s) to exercise: ios = index-only-scan all-visible "
                        "over-read; plain = plain Index Scan same-heap-block over-read "
                        "(default both)")
    p.add_argument("--bindir", help="override the build's bin dir")
    p.add_argument("--datadir", help="scratch data dir (default /tmp/ios_overread_<build>/pgdata)")
    p.add_argument("--port", type=int, default=5599)
    p.add_argument("--io_method", default="worker")
    p.add_argument("--effective_io_concurrency", type=int, default=16)
    p.add_argument("--io_combine_limit", type=int, default=16)
    p.add_argument("--keep-cluster", action="store_true")
    p.add_argument("--reuse-cluster", action="store_true",
                   help="do not initdb/wipe; reuse an existing scratch cluster")
    # Layout knobs
    p.add_argument("--n-tail", type=int, default=200000,
                   help="all-visible tail rows (>> backstop worth of leaf pages)")
    p.add_argument("--pad-bytes", type=int, default=5000,
                   help="dirty-row pad width (wide => ~1 dirty row per heap page)")
    p.add_argument("--fillfactor", type=int, default=50)
    p.add_argument("--num-dirty", type=int, default=None,
                   help="fixed dirty-prefix size (default: T+1 after calibration)")
    # Query / search
    p.add_argument("--calib-limit", type=int, default=1,
                   help="LIMIT used during T/B calibration")
    p.add_argument("--limit-sweep", default="1,2,4,8,16,64,512")
    p.add_argument("--eic-sweep", default="1,16,64,256")
    p.add_argument("--combine-sweep", default="1,16,128")
    p.add_argument("--cache-mode", choices=["cached", "uncached", "both"], default="both")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--search", choices=["none", "full"], default="full")
    # Calibration bounds / thresholds
    p.add_argument("--t-cap", type=int, default=16, help="max D_switch to probe for T")
    p.add_argument("--arm-min", type=int, default=2,
                   help="extra_leaf_reads above this counts as 'armed'")
    p.add_argument("--assumed-backstop", type=int, default=63,
                   help="seed for the layout precondition before B is measured")
    p.add_argument("--b-tail-steps", default="2000,8000,30000,120000,400000")
    # Plain-index-scan layout knobs
    p.add_argument("--plain-arming", type=int, default=None,
                   help="number of WIDE arming rows on distinct heap blocks; MUST equal "
                        "the arming threshold T so the same-block run starts right after "
                        "(default: T from source, else 4)")
    p.add_argument("--plain-limit", type=int, default=None,
                   help="LIMIT for the plain demo (default: near the distance-ramp peak, "
                        "~ T+8 for B~63)")
    p.add_argument("--plain-runlen", type=int, default=150,
                   help="number of same-heap-block run matches (>> B)")
    p.add_argument("--plain-arm-pad", type=int, default=8000,
                   help="wide-row pad for arming rows (=> ~1 row per heap page)")
    p.add_argument("--plain-nomatch-gap", type=int, default=0,
                   help="no-match leaf pages between run matches (0 = OFF; >0 shows the "
                        "read-ahead amplification PAST the backstop -- an accepted, "
                        "partly-inherent risk of any prefetch, NOT a distinct bug)")
    p.add_argument("--single", action="store_true",
                   help="just build the default layout and measure one config")
    return p.parse_args()


def search_ios(cluster, args, sc, t_src, mb_src):
    """IoS full search: self-discover T and B, prove repeatability, sweep LIMIT and
    the I/O GUCs, headline."""
    results = []
    T = B = None
    if args.search == "full":
        T, _ = calibrate_T(cluster, args, sc)
        if T is None:
            print(f"\nWARNING: no arming within D_switch<= {args.t_cap}. The "
                  f"threshold may be outside the designed range; raise --t-cap.")
        else:
            print(f"\n--> discovered arming threshold T = {T}")
            if t_src is not None and T != t_src:
                print(f"    NOTE: differs from source #define ({t_src}) -- "
                      f"stale binary vs source?")
        num_dirty = (args.num_dirty if args.num_dirty else ((T + 1) if T else 6))
        B, _ = calibrate_B(cluster, args, num_dirty, sc)
        print(f"\n--> measured effective backstop B = {B} "
              f"(INDEX_SCAN_MAX_BATCHES ~ {B + 1})")
        if mb_src is not None and (B + 1) != mb_src:
            print(f"    NOTE: B+1={B+1} differs from source #define INDEX_SCAN_MAX_BATCHES"
                  f"={mb_src} -- tail too small or stale binary?")
    else:
        num_dirty = args.num_dirty or 6

    conn = cluster.connect()
    facts = build_layout(conn, num_dirty, args.n_tail, args.pad_bytes, args.fillfactor)
    with conn.cursor() as cur:
        verify_layout(cur, facts, B or args.assumed_backstop)
    print(f"\n[ios] worst-case layout: {facts}")
    verify_repeatable(conn, args.calib_limit, sc)

    modes = (["cached", "uncached"] if args.cache_mode == "both" else [args.cache_mode])
    limits = [int(x) for x in args.limit_sweep.split(",")]
    print("\n== [ios] LIMIT sweep (waste ratio = extra leaf reads / needed) ==")
    for cache in modes:
        for lim in limits:
            m = measure_config(conn, lim, cache, args.runs, sc)
            results.append(m)
            print_row(m)
    print("\n== [ios] I/O-GUC invariance (extra_leaf_reads should stay ~B) ==")
    for eic in [int(x) for x in args.eic_sweep.split(",")]:
        m = measure_config(conn, args.calib_limit, "cached", max(2, args.runs // 2),
                           sc, eic=eic, combine=args.io_combine_limit)
        results.append(m)
        print_row(m)
    for comb in [int(x) for x in args.combine_sweep.split(",")]:
        m = measure_config(conn, args.calib_limit, "cached", max(2, args.runs // 2),
                           sc, eic=args.effective_io_concurrency, combine=comb)
        results.append(m)
        print_row(m)
    conn.close()
    report_headline(results, T if T is not None else t_src,
                    B if B is not None else (mb_src - 1 if mb_src else None),
                    measured=(args.search == "full"))


def search_plain(cluster, args, sc):
    """Plain-scan search: build the worst case and sweep LIMIT to expose the
    over-read.  Below the arming threshold there is no over-read (control); once
    the run-churn lines up with the LIMIT cutoff the over-read jumps to ~the ring
    (INDEX_SCAN_MAX_BATCHES) -- the callback churns same-block batches until it
    finds a different block, bounded only by the ring and by max items per heap
    page -- then declines as the executor consumes the run itself.  The plain case
    is far more parameter-sensitive than IoS, hence the sweep."""
    results = []
    conn = cluster.connect()
    facts = build_plain(conn, args)
    with conn.cursor() as cur:
        verify_layout_plain(cur, facts, args.assumed_backstop)
    print(f"\n[plain] worst-case layout: {facts}")

    modes = (["cached", "uncached"] if args.cache_mode == "both" else [args.cache_mode])
    T = args.plain_arming
    # Bracket the lineup: control (<T), arming, the jump, the peak, the decline.
    limits = sorted({2, T, T + 2, T + 4, T + 6, T + 8, T + 12, T + 20, T + 60})
    print("\n== [plain] LIMIT sweep (control <T; jumps to ~ring when run-churn lines up) ==")
    for cache in modes:
        for lim in limits:
            m = measure_config(conn, lim, cache, args.runs, sc)
            results.append(m)
            print_row(m)
    print("\n== [plain] I/O-GUC invariance (over-read ~ring regardless of eic/combine) ==")
    for eic in [int(x) for x in args.eic_sweep.split(",")]:
        m = measure_config(conn, sc.limit, "cached", max(2, args.runs // 2),
                           sc, eic=eic, combine=args.io_combine_limit)
        results.append(m)
        print_row(m)
    conn.close()

    cached = [r for r in results if r["cache"] == "cached" and r["extra_leaf_reads"]]
    worst = max(cached, key=lambda r: (r["extra_leaf_reads"] or 0)) if cached else None
    bar = "=" * 78
    print("\n" + bar + "\nHEADLINE [plain]")
    if worst:
        print(f"  plain Index Scan, LIMIT {worst['limit']}: read "
              f"{(worst['needed_leaf_pages'] or 0) + (worst['extra_leaf_reads'] or 0):.0f} "
              f"index leaf buffers, only {worst['needed_leaf_pages']:.0f} needed "
              f"(extra {worst['extra_leaf_reads']:.0f}, waste {worst['waste_ratio']:.1f}x).")
        print("  the callback churns same-heap-block batches to find the next block, bounded")
        print(f"  only by INDEX_SCAN_MAX_BATCHES and items/heap-page (run_max_same_block="
              f"{facts['run_max_same_block']}).  Distance is irrelevant.")
        print("  Milder + more parameter-sensitive than IoS: the run-churn must line up with")
        print("  the LIMIT cutoff (IoS churns to ring-full at LIMIT 1, ~B x waste).")
        if args.plain_nomatch_gap > 0:
            print("  With --plain-nomatch-gap>0 the EXTRA exceeds the ring (no-match leaf")
            print("  pages scanned by _bt_readnextpage) -- partly INHERENT to read-ahead, an")
            print("  accepted risk, not a distinct bug; the knob is off by default.")
    else:
        print("  no over-read measured (the run-churn did not line up with this LIMIT range; "
              "widen --limit-sweep or adjust --plain-runlen / run_start).")
    print(bar)


def main():
    args = parse_args()
    if not args.bindir:
        args.bindir = BUILD_BIN[args.build]
    if not args.datadir:
        args.datadir = f"/tmp/ios_overread_{args.build}/pgdata"
    args.b_tail_steps = [int(x) for x in args.b_tail_steps.split(",")]

    if not os.path.isdir(args.bindir):
        sys.exit(f"build bin dir not found: {args.bindir}\n"
                 f"(for --build dc you may need: meson install -C build_meson_dc)")

    t_src, mb_src = grep_source_constants()
    print(f"source constants (cross-check): "
          f"INDEX_PREFETCH_BLKSWITCH_THRESHOLD={t_src} INDEX_SCAN_MAX_BATCHES={mb_src}")

    # Plain scan: arming rows must equal T so the same-block run begins immediately
    # after arming (extra distinct-block arming matches would consume the read
    # stream's distance with useful blocks before it can churn the run).
    if args.plain_arming is None:
        args.plain_arming = t_src or 4
    if args.plain_limit is None:
        # When the read stream asks the callback for the next block to prefetch, the
        # callback churns through same-heap-block batches (the "same block twice in
        # succession" continue, returning nothing) until it finds a DIFFERENT block
        # or hits the ring -- so one "next block" request can read up to
        # INDEX_SCAN_MAX_BATCHES leaf pages.  The only other bound is how many index
        # entries can point at one heap block (max items per heap page).  The
        # prefetch distance is irrelevant.  The LIMIT just needs to be past the
        # point where the prefetch has churned to ring-full; the --no-explain LIMIT
        # sweep shows where that is.
        args.plain_limit = max(12, args.plain_arming + 8)

    scenarios = select_scenarios(args)

    # Default mode: crystal-clear master-vs-patch EXPLAIN comparison (manages its
    # own clusters).  Pass --no-explain for the full calibration/sweep.
    # Gather all variants first, then print ALL plans, then ALL summaries (so the
    # summaries sit together at the end, most prominent).
    if args.explain:
        gathered = [(sc, *explain_gather(args, sc)) for sc in scenarios]
        for sc, blocks, modes in gathered:
            explain_print_plans(sc, blocks, modes)
        for sc, blocks, modes in gathered:
            explain_print_summary(args, sc, blocks, modes)
        return

    cluster = Cluster(args)
    cluster.init(rebuild=not args.reuse_cluster)
    cluster.start()

    # Fail fast (and clean up) if this build predates the prefetch GUC -- the
    # most common cause is a stale install dir.
    conn = cluster.connect()
    with conn.cursor() as cur:
        has_guc = q1(cur, "SELECT 1 FROM pg_settings "
                          "WHERE name='debug_disable_indexscan_prefetch'")
    conn.close()
    if not has_guc and args.build != "master":
        cluster.cleanup()
        sys.exit(f"build at {args.bindir} has no debug_disable_indexscan_prefetch GUC "
                 f"(stale install?).  Refresh it, e.g. "
                 f"`meson install -C {PATCH_ROOT}/build_meson_{args.build}`.")

    log_off = cluster.log_size()
    try:
        for sc in scenarios:
            if args.single:
                conn = cluster.connect()
                facts = sc.build(conn)
                with conn.cursor() as cur:
                    sc.verify(cur, facts, args.assumed_backstop)
                print(f"[{sc.name}] layout: {facts}")
                m = measure_config(conn, sc.limit, "cached", args.runs, sc)
                conn.close()
                print_row(m)
            elif sc.name == "ios":
                search_ios(cluster, args, sc, t_src, mb_src)
            else:
                search_plain(cluster, args, sc)
    finally:
        # dc assertion / crash watch
        new_log = cluster.log_since(log_off)
        trap = [ln for ln in new_log.splitlines()
                if any(s in ln for s in ("TRAP", "assertion", "PANIC", "Assert"))]
        alive = cluster.alive()
        print(f"\nserver alive after run: {alive}; "
              f"assertion/crash lines in log: {len(trap)}")
        for ln in trap[:20]:
            print("   " + ln)
        cluster.cleanup()


def report_headline(results, T, B, measured=True):
    print("\n" + "=" * 78)
    cached = [r for r in results if r["cache"] == "cached" and r["extra_leaf_reads"]]
    worst = max(cached, key=lambda r: (r["waste_ratio"] or 0)) if cached else None
    if worst:
        unc = [r for r in results if r["cache"] == "uncached"
               and r["limit"] == worst["limit"]]
        unc = unc[0] if unc else None
        src = "measured" if measured else "from source #define"
        print("HEADLINE")
        print(f"  arming threshold T = {T};  effective backstop B = {B} "
              f"(INDEX_SCAN_MAX_BATCHES ~ {(B or 0) + 1}) [{src}]")
        print(f"  worst case: LIMIT {worst['limit']} index-only scan read "
              f"{worst['needed_leaf_pages'] + (worst['extra_leaf_reads'] or 0):.0f} "
              f"index leaf pages, only {worst['needed_leaf_pages']:.0f} needed "
              f"(waste ~{worst['waste_ratio']:.0f}x).")
        print(f"  the backstop permits ~{worst['extra_leaf_reads']:.0f} wasted leaf reads.")
        print(f"  cached CPU penalty: {worst['time_abs_ms']:.3f} ms "
              f"({worst['time_ratio']:.2f}x).")
        if unc:
            print(f"  uncached I/O penalty: {unc['time_abs_ms']:.2f} ms "
                  f"({unc['time_ratio']:.2f}x).")
        inv = [r for r in results if r["eic"] is not None or r["combine"] is not None]
        if inv:
            vals = sorted({r["extra_leaf_reads"] for r in inv if r["extra_leaf_reads"]})
            print(f"  over-read invariant to eic/io_combine_limit: "
                  f"extra_leaf_reads in {vals} (structural, not tunable away).")
    print("=" * 78)


if __name__ == "__main__":
    main()
