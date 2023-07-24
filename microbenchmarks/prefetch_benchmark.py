#!/usr/bin/env python3
"""
Prefetch benchmark script for PostgreSQL index scan prefetching.

Compares query performance across three configurations:
1. master (no prefetching, GUC not available)
2. patch with debug_disable_indexscan_prefetch=on (prefetching off)
3. patch with debug_disable_indexscan_prefetch=off (prefetching on)

Usage:
    ./prefetch_benchmark.py                    # Run all queries, uncached, dynamic run count
    ./prefetch_benchmark.py --cached           # Run with data prewarmed
    ./prefetch_benchmark.py --no-cached-index  # Uncached, indexes evicted instead of prewarmed
    ./prefetch_benchmark.py --queries Q1,Q2    # Run specific queries
    ./prefetch_benchmark.py --queries WR1,Q1,GG1  # Queries from several suites at once
    ./prefetch_benchmark.py --runs 5           # Exactly 5 timed runs per query
    ./prefetch_benchmark.py --skip-load        # Skip data loading verification
    ./prefetch_benchmark.py --serialize        # Time output + delivery too (pre-2026-09-25 default)
    ./prefetch_benchmark.py --bitmap           # Same queries as bitmap scans (index_only ones excepted)
    ./prefetch_benchmark.py --verify           # No timing: check both builds use the same plan
"""

import argparse
import atexit
import ctypes
import glob
import json
import math
import mmap
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from statistics import mean, median

import psycopg

# ── Shared configuration & benchmark infrastructure ───────────────────

# --- Configuration ---

# PREFETCH_MASTER_BIN / PREFETCH_PATCH_BIN override the baseline / testbranch
# bindir (scratch installs).
MASTER_BIN = os.environ.get("PREFETCH_MASTER_BIN",
                            "/mnt/nvme/postgresql/master/install_meson_rc/bin")
PATCH_BIN = os.environ.get("PREFETCH_PATCH_BIN",
                           "/mnt/nvme/postgresql/patch/install_meson_rc/bin")
PATCH_DATA_DIR = "/mnt/nvme/postgresql/patch/data"
# master runs against the patch build's data directory, not a copy of its own.
# Both are the same major version and, as long as the patch series carries no
# catalog change, the same catalog version, so master opens the patch cluster
# as-is.  Benchmarking both builds against the very same files also removes
# the physical-layout differences a separately loaded copy would introduce,
# leaves nothing to load or keep in sync (optimizer statistics included), and
# spares a second copy of the data.  A back branch cannot open this cluster at
# all -- a different major version -- so each keeps a dedicated data directory.
# preflight_data_dir() checks every server binary against its data directory
# before anything runs, so a master install left unrebuilt across a catversion
# bump fails at once with a clear message instead of partway through a run.
MASTER_DATA_DIR = PATCH_DATA_DIR
MASTER_SOURCE_DIR = "/mnt/nvme/postgresql/master/source"
PATCH_SOURCE_DIR = "/mnt/nvme/postgresql/patch/source"

REL18_BIN = "/mnt/nvme/postgresql/REL_18_STABLE/install_meson_rc/bin"
REL18_DATA_DIR = "/mnt/nvme/postgresql/REL_18_STABLE/data"
REL18_SOURCE_DIR = "/mnt/nvme/postgresql/REL_18_STABLE/source"

REL19_BIN = "/mnt/nvme/postgresql/REL_19_STABLE/install_meson_rc/bin"
REL19_DATA_DIR = "/mnt/nvme/postgresql/REL_19_STABLE/data"
REL19_SOURCE_DIR = "/mnt/nvme/postgresql/REL_19_STABLE/source"

MASTER_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5555,
}
PATCH_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5432,
}
REL18_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5418,
}
REL19_CONN = {
    "dbname": "regression",
    "user": "pg",
    "host": "/tmp",
    "port": 5419,
}

# Map baseline name to (bin, data_dir, source_dir, conn) for easy lookup
BASELINE_CONFIGS = {
    "master": (MASTER_BIN, MASTER_DATA_DIR, MASTER_SOURCE_DIR, MASTER_CONN),
    "rel18":  (REL18_BIN, REL18_DATA_DIR, REL18_SOURCE_DIR, REL18_CONN),
    "rel19":  (REL19_BIN, REL19_DATA_DIR, REL19_SOURCE_DIR, REL19_CONN),
}

# Map testbranch name to (bin, data_dir, source_dir, conn) for easy lookup
TESTBRANCH_CONFIGS = {
    "patch":  (PATCH_BIN, PATCH_DATA_DIR, PATCH_SOURCE_DIR, PATCH_CONN),
    "master": (MASTER_BIN, MASTER_DATA_DIR, MASTER_SOURCE_DIR, MASTER_CONN),
}

# Whether each build has the debug_disable_indexscan_prefetch GUC
BUILD_HAS_PREFETCH = {
    "patch": True,
    "master": False,
    "rel18": False,
    "rel19": False,
}


# Set by verify_test_has_prefetch() when a testbranch build was expected to have
# the prefetch GUC but didn't.  Re-printed by _print_wall_time() at the very end
# of the run so the warning isn't lost in the scrollback of a long benchmark.
MISSING_PREFETCH_GUC_TESTBRANCH = None


def server_has_prefetch_guc(conn):
    """Return True if the connected server actually defines the
    debug_disable_indexscan_prefetch GUC.  BUILD_HAS_PREFETCH is the *static* declaration
    of what each bindir is expected to provide; this checks the running server."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_settings "
                    "WHERE name = 'debug_disable_indexscan_prefetch'")
        return cur.fetchone() is not None


def print_missing_prefetch_guc_warning(testbranch_name):
    """Print the prominent banner warning that the testbranch build lacks the
    debug_disable_indexscan_prefetch GUC.  Used both inline (at detection) and again at
    the very end of the run."""
    line = "!" * 72
    print(f"\n{line}")
    print(f"!! WARNING: testbranch '{testbranch_name}' has NO "
          f"debug_disable_indexscan_prefetch GUC,")
    print( "!! but BUILD_HAS_PREFETCH expects it.  Benchmarked this build like "
           "a baseline")
    print( "!! (no prefetch=off/on split).  Point the bindir at a patched build "
           "to compare prefetch.")
    print(f"{line}\n")


def verify_test_has_prefetch(conn, test_has_prefetch, testbranch_name,
                             prefetch_disabled):
    """Reconcile the static BUILD_HAS_PREFETCH expectation with the running
    testbranch server.

    If the build is expected to have debug_disable_indexscan_prefetch but the server
    doesn't (e.g. the bindir was rebuilt off a branch without the patch, or
    points at a stock master), print a prominent warning and downgrade to False
    so the build is benchmarked like a baseline -- no prefetch=off/on split --
    instead of erroring out later on SET debug_disable_indexscan_prefetch.  The warning
    is also re-printed at the very end of the run (see _print_wall_time).

    Under --prefetch-disabled (prefetch_disabled) such a build has nothing to
    measure -- only prefetch=off was asked for -- so end the run instead."""
    if not test_has_prefetch or server_has_prefetch_guc(conn):
        return test_has_prefetch
    global MISSING_PREFETCH_GUC_TESTBRANCH
    MISSING_PREFETCH_GUC_TESTBRANCH = testbranch_name
    print_missing_prefetch_guc_warning(testbranch_name)
    if prefetch_disabled:
        print(f"Error: --prefetch-disabled asks for prefetch=off only, and "
              f"'{testbranch_name}' cannot run it; nothing to measure")
        sys.exit(1)
    return False


# --- Result classification (shared by prefetch_benchmark.py + patch_report.py) ---
#
# A patch-vs-baseline per-query result is an improvement / regression / neutral.
# Crucially, a difference that is tiny in ABSOLUTE terms is noise -- classified
# neutral no matter how large the percentage -- so e.g. 0.133ms vs 0.134ms, or
# even 0.133ms vs 0.5ms (a 3.8x ratio, but pure timer noise on a sub-ms query),
# is not a regression.  Callers exclude neutral results from the geometric mean
# and best/worst, since those (often extreme) ratios would otherwise distort it.
RATIO_NEUTRAL_BAND = 0.01   # within +/- 1% ratio -> neutral
NOISE_ABS_MS = 1.0          # abs diff <= this, at ANY query size -> noise -> neutral


def classify_ratio(master_ms, patch_ms, noise_abs_ms=NOISE_ABS_MS):
    """Return 'improvement', 'regression', or 'neutral' for one per-query result.

    A difference within the absolute noise floor (noise_abs_ms, default
    NOISE_ABS_MS; overridable via prefetch_benchmark's --noise-abs-ms) is noise
    -> neutral regardless of the ratio or the query's execution time.  So a
    "regression" requires the patch to be slower both relatively (beyond
    RATIO_NEUTRAL_BAND) and absolutely (more than noise_abs_ms); a sub-floor
    delta is never a regression no matter how large the percentage.
    """
    if not master_ms or master_ms <= 0 or patch_ms is None:
        return "neutral"
    if abs(patch_ms - master_ms) <= noise_abs_ms:
        return "neutral"
    ratio = patch_ms / master_ms
    if ratio < 1.0 - RATIO_NEUTRAL_BAND:
        return "improvement"
    if ratio > 1.0 + RATIO_NEUTRAL_BAND:
        return "regression"
    return "neutral"


# --- Tmpfs Hugepages Functions ---

# Resolve the tmpfs/hugepages helper via $PATH (single source of truth, the same
# script run.sh uses).  Resolve to a full path because mount/umount run under
# sudo, which searches its own secure_path rather than our $PATH -- a bare name
# wouldn't be found there.  Fall back to the bare name if not found so import
# never fails; the error then surfaces at first use.
TMPFS_SCRIPT = shutil.which("pg_tmpfs_hugepages.sh") or "pg_tmpfs_hugepages.sh"


def setup_tmpfs_hugepages():
    """Mount the helper's tmpfs and return the mount point path.

    The helper's stderr is relayed even on success: it says whether the page
    cache had to be dropped to make free 2MB blocks for the copies, and warns
    when there are still too few."""
    result = subprocess.run(
        ["sudo", TMPFS_SCRIPT, "mount", "-s", "800M"],
        capture_output=True, text=True, check=False
    )
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode != 0:
        print("Error: Failed to mount tmpfs (see the helper's output above)")
        sys.exit(1)
    return result.stdout.strip()


def copy_binaries_to_tmpfs(src_bin_dir, tmpfs_mount, version_name):
    """Copy the install tree that src_bin_dir belongs to onto tmpfs, and
    return the copy's bin directory.

    The helper takes the install directory itself, the parent of bin/: it
    puts bin/postgres on 2MB pages and then probes that it really maps that
    way.  Any failure of the helper ends the run, because the run would
    otherwise compare a binary on 2MB pages against one on 4K pages.

    The helper's stderr is relayed even on success: its collapse and probe
    lines are the only evidence that this run's postgres really is
    2MB-backed (run.sh shows the same lines).
    """
    install_dir = os.path.dirname(os.path.normpath(src_bin_dir))
    result = subprocess.run(
        [TMPFS_SCRIPT, "copy", install_dir, version_name],
        capture_output=True, text=True, check=False
    )
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode != 0:
        print(f"Error: could not put the {version_name} postgres binary "
              f"({install_dir}/bin/postgres) on 2MB pages in tmpfs (see the "
              f"helper's output above)")
        sys.exit(1)
    return os.path.join(result.stdout.strip(), "bin")


def cleanup_tmpfs(tmpfs_mount):
    """Unmount the tmpfs filesystem."""
    result = subprocess.run(
        ["sudo", TMPFS_SCRIPT, "umount"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"Warning: Failed to unmount tmpfs: {result.stderr.strip()}")
    elif result.stderr:
        print(result.stderr.strip())


# --- Background Process Interference ---

KILL_INTERFERERS_SCRIPT = "kill_vscode_server.sh"


def kill_interfering_processes():
    """Stop background processes that would skew benchmark measurements.

    Interactive (stdin is a tty): prompt -- you are shown what is running and
    answer y/n once, all-or-nothing.  Answer n to keep those other
    sessions alive while debugging; the benchmark then proceeds with the
    processes still running.  Non-interactive (e.g. cron): --force, killing
    without prompting.
    """
    cmd = [KILL_INTERFERERS_SCRIPT]
    if not sys.stdin.isatty():
        cmd.append("--force")
    result = subprocess.run(cmd, check=False)
    # Exit 2 = you declined at the prompt -> proceed with them running.
    # Exit 1 = --force hit clangd, or a confirmed kill failed -> refuse.
    if result.returncode == 1:
        print("Error: refusing to benchmark with interfering processes running "
              "(see message above). Stop them and retry.")
        sys.exit(1)


CHECK_ENV_SCRIPT = "check_benchmark_env.sh"


def check_benchmark_env():
    """Verify the machine is in a benchmark-friendly state (THP, CPU governor).
    """
    result = subprocess.run(
        [CHECK_ENV_SCRIPT],
        check=False,
    )
    if result.returncode != 0:
        print("Error: benchmark environment check failed or was declined "
              "(see message above). Fix it and retry.")
        sys.exit(1)


# --- NVMe Power State ---
#
# With default power management an idle NVMe drops into a non-operational
# power state after ~100 ms, and the next read pays a fixed wake-up latency of
# several ms.  An uncached run idles the device for at least that long before
# every timed query (eviction, prewarm, OS cache drop), so short queries
# measure the wake-up rather than the I/O: bimodal times, and a large apparent
# regression for whichever side caught the device asleep more often.  Setting
# each controller's PM QoS latency tolerance to 0 forbids the non-operational
# states; "sudo perf_stability.sh prepare --nvme-awake" does that (its
# "restore" puts the default back).  Every uncached run made with the default
# power management ends with a warning, whatever its numbers came out as.
NVME_PM_QOS_GLOB = "/sys/class/nvme/nvme*/power/pm_qos_latency_tolerance_us"
PERF_STABILITY_SCRIPT = "perf_stability.sh"

# Set by note_nvme_power_state(); printed by _print_wall_time() as the very
# last output of the run, like the missing-GUC warning.
NVME_ASLEEP_WARNING = None


def nvme_kept_awake():
    """True if every NVMe controller forbids non-operational power states
    (pm_qos_latency_tolerance_us reads 0), False if any allows them, None if
    there is no NVMe controller or the knob cannot be read."""
    paths = glob.glob(NVME_PM_QOS_GLOB)
    if not paths:
        return None
    for path in paths:
        try:
            with open(path) as f:
                value = f.read().strip()
        except OSError:
            return None
        if value != "0":
            return False
    return True


def print_nvme_asleep_warning(text):
    line = "!" * 72
    print(f"\n{line}")
    for text_line in text.splitlines():
        print(f"!! {text_line}")
    print(f"{line}\n")


def note_nvme_power_state(args, nvme_awake):
    """Arm the end-of-run warning when an uncached run measures with the NVMe
    allowed to sleep.  nvme_awake is nvme_kept_awake() sampled at the start of
    the run.  A cached run's timed queries never reach the device, and None
    (no NVMe, or its state unknown) leaves nothing to say."""
    global NVME_ASLEEP_WARNING
    if args.cached or nvme_awake is not False:
        return
    # Resolve to a full path for the message: sudo searches its own
    # secure_path, not our $PATH.
    script = shutil.which(PERF_STABILITY_SCRIPT)
    script = re.sub(r"^/+", "/", script) if script else PERF_STABILITY_SCRIPT
    NVME_ASLEEP_WARNING = (
        "WARNING: this uncached run used default NVMe power management\n"
        "(pm_qos_latency_tolerance_us is not 0).  An idle NVMe enters a\n"
        "low-power state within ~100 ms and the next read pays a fixed\n"
        "multi-ms wake-up; uncached runs idle the device that long before\n"
        "every timed query, so short queries measure the wake-up, not the\n"
        "I/O.  Treat every ratio above with suspicion.  Hold the device\n"
        "awake and re-run:\n"
        f"    sudo {script} prepare --nvme-awake")


# --- Cache Management Functions ---

def clear_os_cache():
    """Drop the whole OS page cache (plus the dentry and inode slabs)."""
    result = subprocess.run(
        ["sudo", "clear_cache.sh"],
        capture_output=True,
        check=False
    )
    if result.returncode != 0:
        print("Warning: Failed to clear OS cache")


# --- Targeted OS page cache eviction ---------------------------------
#
# Uncached runs need one thing from the OS page cache: the relations a query
# reads from disk must not be cached there.  "sudo clear_cache.sh" buys that by
# writing 3 to /proc/sys/vm/drop_caches, which drops every page in the system
# AND shrinks the dentry and inode slabs.  Measured here, that costs ~490 ms
# whatever is actually cached, and it dominated an uncached run: of a 1073 s
# run, 755 s was per-run setup against 318 s of actual query execution.  The
# cost decomposes as a ~460 ms fixed floor (drop_slab() walking every shrinker
# across all 51 memory cgroups on this box), plus ~435 ms to free the dentry and
# inode slabs, against only ~45 ms to drop 10 GB of page cache -- so the
# benchmark paid roughly ten times over for the one part it wanted.
#
# posix_fadvise(POSIX_FADV_DONTNEED) over just the evicted relations' segment
# files does the same job for the same queries, and needs no sudo.  Its cost is
# proportional to the pages actually resident (0.1 ms once a file is already
# cold), which is the right shape: a query that read little leaves little to
# drop, and a query that read GBs ran long enough that the eviction is noise
# beside it.  The whole-system drop has the opposite shape -- a flat ~490 ms
# tax, ruinous for the 2 ms queries the dynamic run count repeats most often.
# Measured end to end over a 6-query subset at 3 runs each: 47 s -> 29 s, with
# the reported ratios unchanged.
#
# Targeting is also the more faithful measurement: it leaves the postgres
# binary, libc and the catalogs resident instead of making every timed query
# re-fault them.  It does change what an uncached number means, though, so
# results carry an "os_cache_eviction" field and must not be compared across it.
#
# Only the main fork is dropped: the visibility map is prewarmed on purpose and
# is read on an index-only scan's measured path, and the free space map is never
# read here.  The whole-system drop could not spare either of them.
#
# DONTNEED silently keeps dirty pages, so each file is flushed first (~4 ms;
# the benchmark relations are read-only once loaded).  Because a silent failure
# here would quietly turn an uncached run into a warm one, the first eviction
# after each server start verifies with mincore() that the pages really left,
# and falls back to clear_os_cache() for the rest of the run if they did not.
# --global-drop-caches forces the old whole-system behavior outright.
GLOBAL_DROP_CACHES = False       # set from args; True restores clear_os_cache()

_os_evict_files = {}             # relname -> [absolute file paths]
_os_evict_datadir = None         # the running server's data_directory
_os_evict_ok = None              # None: unverified, True: works, False: fell back


def _relation_segment_files(conn, relname):
    """Absolute paths of *relname*'s MAIN fork: every 1 GB segment of it.

    The main fork is the only thing an uncached run wants cold.  The visibility
    map is deliberately kept warm -- run_query() prewarms it into shared buffers
    on purpose, and an index-only scan reads it on the measured path -- so
    dropping its file pages would only risk putting an I/O back into the timed
    query.  The free space map is never read by these queries at all.  Both are
    therefore left alone, which the whole-system drop this replaced could not do.

    PostgreSQL names the files <relfilenode>[_fork][.segment], so a plain
    "<relfilenode>*" glob would also match the other forks and, worse, unrelated
    relfilenodes sharing the prefix (16385 vs 163850).  Match the real grammar
    instead, and keep only the unsuffixed main-fork segments.
    """
    global _os_evict_datadir

    if _os_evict_datadir is None:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            _os_evict_datadir = cur.fetchone()[0]

    # Only one server runs at a time, and stop_server() clears this cache, so a
    # single data directory and one path map per relation are enough.  A cached
    # path list is still re-resolved if any file under it has gone: a relation
    # rewritten between runs gets a new relfilenode.
    cached = _os_evict_files.get(relname)
    if cached and all(os.path.exists(f) for f in cached):
        return cached

    with conn.cursor() as cur:
        cur.execute("SELECT pg_relation_filepath(%s)", (relname,))
        row = cur.fetchone()
    if not row or not row[0]:
        return []
    relpath = os.path.join(_os_evict_datadir, row[0])
    directory, node = os.path.split(relpath)
    pattern = re.compile(r"^%s(\.\d+)?$" % re.escape(node))
    try:
        files = sorted(os.path.join(directory, e.name)
                       for e in os.scandir(directory) if pattern.match(e.name))
    except OSError as e:
        print(f"Warning: cannot list {directory} to evict {relname}: {e}")
        return []
    _os_evict_files[relname] = files
    return files


_libc = None


def _mincore_libc():
    """libc with mmap/munmap/mincore prototyped, or None where that is not
    possible.  Built once: ctypes.CDLL(None) is a dlopen every time."""
    global _libc
    if _libc is None:
        try:
            lib = ctypes.CDLL(None, use_errno=True)
            lib.mmap.restype = ctypes.c_void_p
            lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_long]
            lib.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            lib.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_ubyte)]
            _libc = lib
        except (OSError, AttributeError):
            _libc = False
    return _libc or None


def _resident_bytes(path):
    """Bytes of *path* currently resident in the OS page cache, or None if the
    question could not be asked.  Uses mincore(2), which only reports residency
    -- it never faults a page in.  Goes through libc rather than Python's mmap
    because ctypes cannot take the address of a read-only mmap buffer."""
    libc = _mincore_libc()
    if libc is None:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return 0
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        addr = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
    finally:
        os.close(fd)
    if not addr or addr == (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1:
        return None                      # MAP_FAILED
    try:
        npages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
        vec = (ctypes.c_ubyte * npages)()
        if libc.mincore(ctypes.c_void_p(addr), size, vec) != 0:
            return None
        # Linux sets bit 0 for a resident page and leaves the rest zero; count
        # the non-zero bytes in C rather than looping over millions of pages.
        buf = bytes(memoryview(vec))
        return (len(buf) - buf.count(0)) * mmap.PAGESIZE
    finally:
        libc.munmap(ctypes.c_void_p(addr), size)


def _fadvise_dontneed(files):
    """Flush, then drop, the OS page cache backing *files*."""
    for path in files:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue                     # segment vanished; nothing cached either
        try:
            try:
                os.fsync(fd)             # DONTNEED keeps dirty pages
            except OSError:
                pass
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def evict_os_cache(conn, relations):
    """Make *relations* cold in the OS page cache.

    Targets just those relations' files (see the block comment above), and falls
    back to dropping the whole system page cache if that is asked for, if the
    relations cannot be resolved, or if the first attempt of a run leaves pages
    resident."""
    global _os_evict_ok

    if GLOBAL_DROP_CACHES or _os_evict_ok is False:
        clear_os_cache()
        return

    files = []
    unresolved = []
    for rel in relations:
        found = _relation_segment_files(conn, rel)
        files.extend(found)
        if not found:
            unresolved.append(rel)
    if unresolved:
        # A relation we cannot name on disk is a relation we cannot evict, and
        # leaving it warm would quietly turn this into a cached measurement.
        print(f"Warning: cannot locate the files of {', '.join(unresolved)}; "
              f"dropping the whole page cache for the rest of this run")
        _os_evict_ok = False
        clear_os_cache()
        return
    if not files:
        return                           # query lists nothing to evict

    _fadvise_dontneed(files)

    if _os_evict_ok is None:
        # First eviction of the run: prove the pages actually left before
        # trusting the cheap path for every remaining query.
        resident = 0
        measured = False
        for path in files:
            got = _resident_bytes(path)
            if got is not None:
                resident += got
                measured = True
        if measured and resident > 16 * 1024 * 1024:
            print(f"Warning: posix_fadvise left {resident / 2**20:.0f} MB of "
                  f"{', '.join(relations)} resident; falling back to dropping "
                  f"the whole page cache for the rest of this run")
            _os_evict_ok = False
            clear_os_cache()
        else:
            _os_evict_ok = True


def reset_os_evict_state():
    """Forget the resolved file paths and the verification verdict.  Called when
    a server stops: the next one may run a different data directory, and its
    first eviction should be verified afresh."""
    global _os_evict_ok, _os_evict_datadir
    _os_evict_files.clear()
    _os_evict_datadir = None
    _os_evict_ok = None


def evict_relations(conn, relations):
    """Evict relations from PostgreSQL buffer cache."""
    max_retries = 3
    with conn.cursor() as cur:
        for rel in relations:
            for attempt in range(max_retries):
                try:
                    cur.execute(f"SELECT * FROM pg_buffercache_evict_relation('{rel}')")
                    row = cur.fetchone()
                    if row:
                        buffers_evicted, buffers_flushed, buffers_skipped = row
                        if buffers_skipped > 0:
                            if attempt < max_retries - 1:
                                time.sleep(0.1)
                                continue  # Retry
                            print(f"Warning: Failed to evict {buffers_skipped} buffers from {rel} "
                                  f"after {max_retries} attempts "
                                  f"(evicted={buffers_evicted}, flushed={buffers_flushed})")
                    break  # Success or no row returned
                except Exception as e:
                    conn.rollback()
                    print(f"Warning: Failed to evict {rel}: {e}")
                    break


def prewarm_relations(conn, relations, include_vm=False, vm_only=False):
    """Prewarm relations into PostgreSQL buffer cache.

    If include_vm is True, also prewarm the visibility map fork.
    If vm_only is True, prewarm ONLY the visibility map fork (not main).
    include_vm should be set for heap relations (tables) but NOT for indexes,
    which have no VM fork.
    """
    with conn.cursor() as cur:
        for rel in relations:
            try:
                if not vm_only:
                    cur.execute(f"SELECT pg_prewarm('{rel}')")
                if include_vm or vm_only:
                    cur.execute(f"SELECT pg_prewarm('{rel}', 'buffer', 'vm')")
            except Exception as e:
                conn.rollback()
                print(f"Warning: Failed to prewarm {rel}: {e}")


def disable_prefetch_guc_value(prefetch):
    """Map a prefetch on/off setting to the value to use for the
    debug_disable_indexscan_prefetch GUC, whose meaning is inverted (on means
    that prefetching is disabled)."""
    return "off" if str(prefetch).lower() in ("on", "true", "1") else "on"


def set_gucs(conn, gucs, is_master=False, prefetch_setting=None):
    """Set GUCs for query execution."""
    with conn.cursor() as cur:
        # Base GUCs for all configurations
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")
        # EXECUTE is a utility statement, so the server materializes the whole
        # result set in a tuplestore (PORTAL_UTIL_SELECT) before sending any of
        # it.  Keep that tuplestore in memory for every query: the largest
        # result set (A39: 11.3M rows) spills to temp files at the default
        # 100MB, and the timing then measures the spill and its readback more
        # than the scan (2.03s spilling vs 1.65s in memory; 1GB suffices,
        # 2GB leaves margin).  Same value on baseline and patch.
        cur.execute("SET work_mem = '2GB'")

        # Set prefetch GUC only on patch (not master)
        if not is_master and prefetch_setting is not None:
            cur.execute("SET debug_disable_indexscan_prefetch = "
                        f"{disable_prefetch_guc_value(prefetch_setting)}")

        # Query-specific GUCs
        for guc, value in gucs.items():
            cur.execute(f"SET {guc} = {value}")


def reset_gucs(conn, gucs):
    """Reset query-specific GUCs to defaults."""
    with conn.cursor() as cur:
        for guc in gucs:
            cur.execute(f"RESET {guc}")


def bitmap_query_def(query_def):
    """--bitmap: rewrite one suite query definition so the planner turns its
    index scans into bitmap heap scans.

    The forcing rides the per-query "gucs" entry, which every path that plans
    a query (prepare_query(), run_query(), capture_plan()) already applies --
    and applies AFTER set_gucs()'s base GUCs, so the enable_bitmapscan=on here
    wins over the base enable_bitmapscan=off.  Assigning into the dict also
    replaces a suite's own enable_bitmapscan="off" entry.  enable_seqscan
    stays off from the base GUCs, leaving bitmap as the only heap path.

    Queries marked index_only are returned unchanged: they measure pure
    index-only scans, which --bitmap deliberately leaves alone.  A query NOT
    so marked loses any index-only scan in its plan to a bitmap scan as well:
    enable_indexscan=off disables index-only scan paths too (the path
    generation strategy mask in standard_planner()), and no GUC can bitmap
    one scan in a plan but spare another.

    Returns a copy; the shared suite definition is never modified.
    """
    if query_def.get("index_only", False):
        return query_def
    query_def = dict(query_def)
    gucs = dict(query_def.get("gucs", {}))
    gucs["enable_indexscan"] = "off"
    gucs["enable_bitmapscan"] = "on"
    query_def["gucs"] = gucs
    return query_def


def bitmap_queries_dict(queries_dict):
    """--bitmap: bitmap_query_def() applied to a whole suite queries mapping."""
    return OrderedDict(
        (qid, bitmap_query_def(qd)) for qid, qd in queries_dict.items())


# Benchmark suites are defined declaratively in suites/*.toml and loaded by
# benchmark_suites.py, which owns the single source of truth.  BENCHMARK_SUITES is
# re-exported here so existing consumers (e.g. perf_flamegraph.py) keep importing it
# from prefetch_benchmark.
from benchmark_suites import (
    BENCHMARK_SUITES, build_query_suite_map, ensure_all_visible_after_load,
)

# --help presentation for suite groups.  A suite's group is the suites/<group>/
# subdirectory it lives in (None for top-level suites).  Maps a group id to its
# (argument-group title, description); unmapped groups fall back to "<id> suites".
SUITE_GROUP_HELP = {
    "regression": (
        "regression suites (read-stream concerns)",
        "Known regressions of particular concern -- read-stream / readahead "
        "behavior.  Each loads its own data if needed.",
    ),
    "workloads": (
        "workload suites (index types & scan patterns)",
        "Benchmarks exercising assorted index access methods and scan patterns "
        "(UUID, index-only heap fetches, GiST, SP-GiST, hash).  Each loads its "
        "own data if needed.",
    ),
}

# Output directory for results, beside this script whatever the current
# directory (patch_report.py reads the same one).  PREFETCH_OUTPUT_DIR
# overrides where results are read from and written to.
OUTPUT_DIR = os.environ.get(
    "PREFETCH_OUTPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "prefetch_results"))

# Global flag for using median vs min (set from args)
USE_MEDIAN = False


def get_representative(times):
    """Return the representative value (median or min) from a list of times."""
    if not times:
        return None
    return median(times) if USE_MEDIAN else min(times)


def get_stat_label():
    """Return the label for the representative statistic."""
    return "median" if USE_MEDIAN else "min"


# Dynamic per-query run count (see measure_query()).  We run RUNS_MIN times,
# then keep running until the total per-run WALL time -- cache eviction + prewarm
# + the timed query, not just the query -- reaches --runs-budget (default
# RUNS_BUDGET_MS), capped at RUNS_MAX.  RUNS_MIN is a hard floor.  Counting the
# eviction is deliberate: a query whose per-run wall is >= budget/3 gets just 3
# runs, so uncached queries with expensive heap eviction are not run dozens of
# times; cheaper-per-run queries (cached / index_only, whose only repeated cost
# is the query) extend toward the budget.  Only PER-RUN costs count: the one-time
# cache preparation of cached / index_only queries happens before the first run
# and outside the wall clock, since prewarming one large heap (Q1: ~1.7 s) would
# otherwise exhaust the budget by itself and hold a cheap query at the floor.
# RUNS_MAX caps the very cheap queries (whose tiny per-run wall would otherwise
# let the budget buy hundreds of runs) at a point where the min has long since
# converged.
#
# A complementary EXECUTION-time budget (RUNS_EXEC_BUDGET_MS, --runs-exec-budget)
# runs alongside the wall budget: we keep going only while the total PURE QUERY
# time (the sum of the measured per-run times, eviction excluded) is ALSO below
# it.  The two are AND'd -- the loop stops as soon as EITHER budget is met.  A
# cheap query can never meet an execution budget quickly, so OR'ing the two let
# it defeat the wall budget and run to RUNS_MAX however expensive its per-run
# eviction was.  Measured over the result history, those extra runs bought no
# reproducibility at all: across repeat invocations of the same binaries the mean
# run-to-run CV of the reported ratio was 1.56% using every run and 1.63% using
# only the first three, and for the cheapest queries (A4, A37, A42) taking the min
# over ~60 runs was several times WORSE than over 3 -- min walks further into the
# left tail the longer it looks, and for an uncached query that tail is mostly
# runs where the eviction did not fully take.  Both budgets are capped at RUNS_MAX.
RUNS_MIN = 3
RUNS_MAX = 60
RUNS_BUDGET_MS = 800.0
RUNS_EXEC_BUDGET_MS = 500.0

# Untimed warm-up runs (see measure_query.warm_up()).  Two things make a query's
# first runs unrepresentative.  A slow first run is routine: over the result
# history, run 0 is at least 5% above the rest of its phase in 37% of
# query-phases (median 1.028x, p90 1.33x).  And a few queries carry a real
# transient -- ZR2's baseline decays 6.76 -> 4.42 ms over roughly 40 runs, and
# one recorded invocation had Q9 start at 51/57/47 ms before settling at its
# usual 19.5 -- so their first samples measure the climb rather than the query.
#
# Rather than name those queries and give each a count, warm up on measured
# behavior: keep running untimed until the last WARMUP_SETTLE_RUNS runs agree
# within WARMUP_SETTLE_PCT, which a flat query reaches immediately and a
# transient one does not.  Both caps are on cost, not on a per-query setting.
#
# WARMUP_MAX_QUERY_MS is what keeps this free for slow queries: the first run is
# the probe, and a query slower than this keeps that run as its first timed
# sample instead of discarding it.  So the expensive queries -- which stop at the
# RUNS_MIN floor precisely because one run costs seconds -- do no extra work.
# It has to sit well clear of the first-run times of the queries that are meant
# to warm up, though: a query whose first run straddles it warms up in some
# invocations and not in others.  Q8's first run in a fresh backend is 48-61 ms,
# which is why this is 100 rather than 50.
WARMUP_MAX = 20              # never spend more than this many untimed runs
WARMUP_BUDGET_MS = 500.0     # nor more than this much wall time on them
WARMUP_SETTLE_RUNS = 3       # this many consecutive runs must agree ...
WARMUP_SETTLE_PCT = 1.5      # ... to within this percent of their median
WARMUP_MAX_QUERY_MS = 100.0  # queries slower than this never warm up at all

# Queries too slow to warm up can still get an unrepresentative first run, and
# there it is usually unrepresentatively FAST.  Measured over the result history,
# run 0 lands more than 5% BELOW the median of the rest of its phase in 62.8% of
# query-phases in the 300 ms - 2 s band (median ratio 0.858), against 0% above
# 2 s and under 8% below 300 ms.  A41 is the extreme: 0.792 over 94 phases, which
# flips it between a 1.25x regression and a 0.92x improvement depending on
# whether the baseline caught a fast first run.
#
# The cause is not the benchmark's own cache handling.  With A41 the fast run
# appears only when an earlier query read the whole prefetch_orders heap, and it
# survives evicting that heap from shared buffers, evicting its file pages by
# either posix_fadvise or a whole-system drop_caches, and dropping the dentry and
# inode slabs -- while pg_buffercache reports zero resident buffers and mincore
# reports zero resident file pages.  Under EXPLAIN every run reads the same 4648
# blocks and spends the same ~651 ms waiting on them.  Whatever carries it lives
# below the host, so the benchmark cannot evict it; it can only decline to
# measure it.  The steady-state number is the honest one -- with nothing read
# beforehand A41 is ~669 ms on every run, and ~490 ms only after that full read.
#
# So discard a first timed run that disagrees with the rest of its own phase and
# take one more in its place.  This costs one extra run and only when it fires,
# which keeps it off the queries that are expensive precisely because a run costs
# seconds: above 2 s, run 0 is within 0.5% of the rest at the median.
FIRST_RUN_OUTLIER_PCT = 5.0  # replace run 0 when it differs from the rest by this

# CPU pinning settings.  Whether the backend is pinned at all follows the mode
# (on with --cached, off otherwise; --pin / --no-pin force it): see
# pinning_summary() and pin_backend().  When pinning is on, this process (the
# benchmark client: psycopg/libpq drain the timed EXECUTEs in-process) is
# pinned too -- an unpinned client is a per-invocation scheduler lottery, and
# it can even land on BENCHMARK_CPU, where the SCHED_FIFO backend starves it
# until the socket buffer fills.  CLIENT_CPU sits in the same L3 domain as
# BENCHMARK_CPU on this machine (5950X: CCD1 = CPUs 8-15) so the Unix-socket
# handoff stays within one L3, but off the backend's core.
BENCHMARK_CPU = 14
CLIENT_CPU = 12

# The affinity mask this process started with, captured at import time.
# start_server() launches every postmaster with this mask: a pinned client
# must not leak its one-CPU affinity into pg_ctl's children (the postmaster,
# every backend, and the io workers would all inherit it).
_ORIG_AFFINITY = frozenset(os.sched_getaffinity(0))

# --- Stress-test query generation probabilities ---
# These control the likelihood of various query features in randomly generated queries.
# Tune these to focus on patterns most likely to expose regressions.

STRESS_PROB_LATERAL_JOIN = 0.15        # Use LATERAL subquery (top-N per group)
STRESS_PROB_ANTI_JOIN = 0.10           # Use NOT EXISTS anti-join
STRESS_PROB_SEMI_JOIN = 0.15           # Use EXISTS semi-join
STRESS_PROB_CORRELATED_SUBQUERY = 0.10 # Correlated subquery in SELECT clause
STRESS_PROB_SELF_JOIN = 0.10           # Self-join on orders (exercises merge join mark/restore)
STRESS_PROB_FILTER_QUAL = 0.25         # Add filter qual that can't use index
STRESS_PROB_ORDER_BY = 0.70            # Include ORDER BY clause
STRESS_PROB_LIMIT = 0.50               # Include LIMIT clause (when ORDER BY present)
STRESS_PROB_AGGREGATE = 0.20           # Use count(*) or sum() aggregate
STRESS_PROB_INDEX_ONLY = 0.15          # Query only columns in index (index-only scan)
STRESS_PROB_MULTI_TABLE_JOIN = 0.30    # JOIN to dimension tables
STRESS_PROB_BACKWARDS_SCAN = 0.10      # Use DESC ordering (backwards index scan)
STRESS_PROB_IN_LIST = 0.15             # Use IN (...) instead of BETWEEN
STRESS_PROB_HIGH_SELECTIVITY = 0.30    # Very selective (few rows)
STRESS_PROB_LOW_SELECTIVITY = 0.20     # Low selectivity (many rows)
STRESS_PROB_SINGLE_DATE = 0.20         # Use exact date equality (more zero-row inner scans)
STRESS_PROB_FORCE_MERGE = 0.20         # Force MergeJoin on joins (disable hashjoin+nestloop)
STRESS_PROB_SORT_NON_INDEXED = 0.15    # ORDER BY non-indexed column (forces Sort above scan)

# Stress-test configuration
STRESS_QUERIES_PER_BATCH = 10          # Number of queries to generate per iteration
STRESS_REGRESSION_THRESHOLD = 1.05     # 5% slower = regression
STRESS_MIN_QUERY_MS = 1.5             # Replace queries faster than this (too noisy)

# --- Stress-test novelty bias ---
# Every generated query carries a set of shape tags: the words in its
# "Stress #N: ..." name, drawn from STRESS_TAGS.  STRESS_KNOWN_BAD lists the
# shapes that stress mode has already caught, or that a suite measures on every
# run anyway, each with the suite queries it stands for.  A regression in one of
# these shapes shows up in the suite results without any help from stress mode,
# so a stress draw that lands there tells us nothing new.  generate_random_query()
# rejects a candidate whose tags contain a listed shape with probability
# STRESS_AVOID_KNOWN ** (1 + extra), extra being the tags the candidate adds on
# top of the closest listed shape, and redraws: an exact repeat is rejected
# hardest, a decorated variant less, and an unlisted shape never.  Sizes
# (selectivity, LIMIT value, date span) are deliberately not tags: a bigger copy
# of a known shape is still that shape.  Add an entry here when a stress
# regression is promoted into a suite; --stress-avoid adds entries for one run
# and --stress-avoid-known 0 turns the bias off.
STRESS_TAGS = frozenset({
    "LATERAL", "anti-join", "semi-join", "correlated", "aggregate", "JOIN",
    "multi-JOIN", "customers", "products", "self-join", "range-scan",
    "date-idx", "cust-date-idx", "product-idx", "index-only", "filter",
    "order-by", "limit", "backwards", "in-list", "single-date",
    "sort-non-indexed", "merge-forced",
})
STRESS_KNOWN_BAD = [
    # LATERAL top-N per customer: forward, backward, single-date probe
    ({"LATERAL"},                                             "A8 A26 A41"),
    ({"LATERAL", "backwards"},                                "Q9 A10 A18"),
    ({"LATERAL", "single-date"},                              "ZR1"),
    # NOT EXISTS anti-joins with orders on the outer side
    ({"anti-join", "index-only"},                             "A11 A12"),
    ({"anti-join", "order-by", "limit"},                      "A20"),
    # correlated subplan in the SELECT list
    ({"correlated"},                                          "A16"),
    # GROUP BY order_date aggregates: index-only, and filtered (plain scan)
    ({"aggregate"},                                           "A17"),
    ({"aggregate", "filter"},                                 "A30"),
    # dimension-table joins
    ({"JOIN", "customers", "order-by"},                       "Q2"),
    ({"JOIN", "customers", "order-by", "backwards"},          "Q5"),
    ({"JOIN", "customers", "filter", "merge-forced", "single-date"}, "A42"),
    ({"JOIN", "products", "order-by"},                        "A27"),
    # self-join (merge join mark/restore)
    ({"self-join"},                                           "MR1 MR2 MR3 MR4"),
    # range scans, by index
    ({"range-scan", "date-idx", "order-by", "limit"},         "Q1"),
    ({"range-scan", "date-idx", "index-only"},                "A25 (dropped; same mechanism as A39)"),
    ({"range-scan", "cust-date-idx"},                         "Q7 A36 A37"),
    ({"range-scan", "cust-date-idx", "in-list"},              "Q4 A38"),
    ({"range-scan", "cust-date-idx", "index-only"},           "A19"),
    ({"range-scan", "product-idx"},                           "A28"),
    ({"range-scan", "product-idx", "order-by", "limit"},      "A32"),
    ({"range-scan", "product-idx", "filter", "order-by", "limit"}, "A34"),
    ({"range-scan", "product-idx", "index-only"},             "A14 A39"),
]
for _shape, _origin in STRESS_KNOWN_BAD:
    assert _shape <= STRESS_TAGS, f"STRESS_KNOWN_BAD ({_origin}): unknown tag(s) {_shape - STRESS_TAGS}"
STRESS_AVOID_KNOWN = 0.8               # base rejection probability (--stress-avoid-known)
STRESS_AVOID_MAX_REDRAWS = 20          # then keep the candidate anyway


# io_max_concurrency is pinned high (below the io_uring per-ring ceiling) so that
# effective_io_concurrency is never clamped, for any io_method. It is not a CLI
# knob -- there is no benchmark reason to vary it; start_server() applies it.
IO_MAX_CONCURRENCY = 512

# Per-io-method defaults for the io-method-varying GUCs. Applied by
# resolve_io_method_defaults() only to options the user did NOT pass explicitly
# (argparse default=None sentinel). io_max_combine_limit is pinned to its maximum
# in start_server() and io_max_concurrency to IO_MAX_CONCURRENCY, so neither
# appears here. The worker-only knobs (io_max_workers, io_min_workers,
# io_worker_launch_interval) are inert under io_uring/sync but kept uniform.
IO_METHOD_DEFAULTS = {
    "io_uring": {
        "effective_io_concurrency": 100,
        "io_combine_limit": 16,
        "io_max_workers": 8,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
    "worker": {
        "effective_io_concurrency": 100,
        "io_combine_limit": 16,
        # Tuned: the io worker pool is the I/O-parallelism ceiling for
        # io_method=worker. At the stock 8, eic=100 was bottlenecked; raising it to
        # 32 (MAX_IO_WORKERS) cut the prefetch-suite geomean ~18% (0.451 -> 0.370)
        # and improved WR1/MU1/TD2 vs rel18. eic/icl/ilw stayed stock (raising
        # them hurt MU1/TD2).
        "io_max_workers": 32,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
    "sync": {
        "effective_io_concurrency": 0,
        "io_combine_limit": 16,
        "io_max_workers": 8,
        "io_min_workers": 2,
        "io_worker_launch_interval": 100,
    },
}


def resolve_io_method_defaults(args):
    """Fill any io GUC left as None (user did not pass it) from the per-io-method
    default table, keyed on args.io_method. An explicit flag value always wins."""
    table = IO_METHOD_DEFAULTS.get(args.io_method, IO_METHOD_DEFAULTS["io_uring"])
    for key, val in table.items():
        if getattr(args, key) is None:
            setattr(args, key, val)
    return args


def add_io_server_args(parser):
    """Register the server-start io GUC knobs.  Shared by prefetch_benchmark and
    perf_flamegraph so both harnesses start servers with identical settings --
    these feed start_server() and resolve_io_method_defaults().  Any knob left as
    None (sentinel) is filled per io_method by resolve_io_method_defaults(); an
    explicit value on the command line always wins."""
    parser.add_argument(
        "--effective_io_concurrency",
        type=int,
        default=None,
        help="effective_io_concurrency GUC (default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_combine_limit",
        type=int,
        default=None,
        help="io_combine_limit in 8kB units (default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_max_workers",
        type=int,
        default=None,
        help="io_max_workers GUC, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_min_workers",
        type=int,
        default=None,
        help="io_min_workers GUC, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_worker_launch_interval",
        type=int,
        default=None,
        help="io_worker_launch_interval GUC in ms, io_method=worker only "
             "(default: per io_method, see IO_METHOD_DEFAULTS)"
    )
    parser.add_argument(
        "--io_method",
        type=str,
        default="io_uring",
        choices=["io_uring", "worker", "sync"],
        help="Value for io_method PostgreSQL setting (default: io_uring)"
    )
    parser.add_argument(
        "--direct-io",
        action="store_true",
        dest="direct_io",
        help="Start Postgres with debug_io_direct=data and skip the OS page cache "
             "eviction (O_DIRECT bypasses the OS page cache)"
    )
    parser.add_argument(
        "--patch-direct",
        action="store_true",
        dest="patch_direct",
        help="Like --direct-io, but only for the testbranch server; the "
             "baseline keeps buffered I/O.  Direct I/O only makes sense with "
             "prefetching, so a direct-I/O baseline isn't a fair comparison "
             "point"
    )


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Prefetch benchmark for PostgreSQL index scan prefetching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                         # Run all queries, uncached, dynamic run count
    %(prog)s --cached                # Run with data prewarmed
    %(prog)s --queries Q1,Q2,A1      # Run specific queries
    %(prog)s --queries WR1,Q1,GG1    # Queries spanning several suites at once
    %(prog)s --runs 5                # Exactly 5 timed runs per query
    %(prog)s --skip-load             # Skip data loading verification
    %(prog)s --verify --queries Q1   # No timing: check both builds use the same plan
        """
    )
    # ----- workload selection: what to run, under what cache state ----- #
    g_workload = parser.add_argument_group("workload selection")
    g_workload.add_argument(
        "--cached",
        action="store_true",
        help="Run in cached mode (prewarm all relations instead of just indexes)"
    )
    g_workload.add_argument(
        "--no-cached-index",
        action="store_true",
        help="Uncached run with cold indexes too: each query's indexes are "
             "evicted from shared buffers and the OS page cache, exactly like "
             "the heap, instead of being prewarmed. index_only queries, which "
             "normally skip eviction entirely, get their index evicted as "
             "well. The heap visibility map stays prewarmed as always. "
             "Results go to a separate '<suite>_noidx_uncached' mode, so they "
             "never mix with warm-index uncached numbers. Cannot be combined "
             "with --cached."
    )
    g_workload.add_argument(
        "--queries",
        type=str,
        default=None,
        help="Comma-separated list of queries to run (e.g., Q1,Q2,A1). May span "
             "multiple suites (e.g. WR1,Q1,GG1); each owning suite is run with "
             "its subset, no suite flag needed. Default: all queries of the "
             "selected suite"
    )
    g_workload.add_argument(
        "--bitmap",
        action="store_true",
        help="Force bitmap scans: plan every query with enable_indexscan=off "
             "and enable_bitmapscan=on, turning its index scans into bitmap "
             "heap scans. Queries marked index_only in their suite keep the "
             "standard forcing GUCs and still run index-only scans. A query "
             "NOT so marked has any index-only scan in its plan forced to a "
             "bitmap scan too: enable_indexscan=off disables index-only scan "
             "paths as well, and no finer-grained GUC exists. Results go to a "
             "separate '<suite>_bitmap_<cached|uncached>' mode, so bitmap "
             "numbers never mix with index-scan ones. Not supported with "
             "--stress-test."
    )
    # Suite-selection flags (auto-generated from BENCHMARK_SUITES registry).
    # Suites that share a group (a suites/<group>/ subdirectory) get their own
    # labelled section in --help; ungrouped suites stay under workload selection.
    suite_arg_groups = {}  # group id -> argparse argument group
    for suite in BENCHMARK_SUITES:
        if suite["cli_flag"] is None:
            continue
        grp = suite["group"]
        if grp is None:
            target = g_workload
        else:
            if grp not in suite_arg_groups:
                title, desc = SUITE_GROUP_HELP.get(grp, (f"{grp} suites", None))
                suite_arg_groups[grp] = parser.add_argument_group(title, desc)
            target = suite_arg_groups[grp]
        target.add_argument(
            suite["cli_flag"],
            action="store_true",
            dest=suite["cli_dest"],
            help=suite["help"],
        )

    # ----- sampling: how many timed runs per query ----- #
    g_runs = parser.add_argument_group("sampling (run count)")
    g_runs.add_argument(
        "--runs", "--force-runs",
        dest="runs",
        type=int,
        default=None,
        metavar="N",
        help="Force exactly N runs per query, bypassing the dynamic run-count "
             "logic (even when N < 3). Default (unset): dynamic -- a minimum of 3 "
             "runs, extended until --runs-budget (total wall time: eviction + "
             "query) OR --runs-exec-budget (pure query time) is reached, whichever "
             f"comes first (up to {RUNS_MAX}). (--force-runs is a backward-compatible "
             "alias.)"
    )
    g_runs.add_argument(
        "--runs-budget",
        dest="runs_budget",
        type=float,
        default=RUNS_BUDGET_MS,
        metavar="MS",
        help=f"Target total per-query wall-clock (ms) the dynamic run-count "
             f"spends before stopping (default: {RUNS_BUDGET_MS:.0f}) -- counting "
             f"per-run cache eviction + prewarm + the timed query, not just the "
             f"query, so queries with expensive uncached eviction are not over-run "
             f"(the one-time prewarm of cached and index-only queries is not "
             f"counted). AND'd with "
             f"--runs-exec-budget (the loop stops as soon as EITHER is met). Clamped "
             f"to [{RUNS_MIN}, {RUNS_MAX}] runs. Ignored when an exact --runs count "
             f"is given."
    )
    g_runs.add_argument(
        "--no-warmup",
        action="store_true",
        dest="no_warmup",
        help="Skip the untimed warm-up runs. By default a query whose first run "
             f"is at or below {WARMUP_MAX_QUERY_MS:.0f} ms keeps running untimed "
             f"until {WARMUP_SETTLE_RUNS} consecutive runs agree within "
             f"{WARMUP_SETTLE_PCT:g}%% (at most {WARMUP_MAX} runs or "
             f"--warmup-budget), so the timed runs measure steady state rather "
             "than a warm-up climb. Queries slower than that keep their first "
             "run as a timed sample and never warm up, so nothing expensive "
             "pays for this. The testbranch repeats the baseline's warm-up run "
             "count for each query rather than deciding for itself, so both "
             "sides' timed runs start at the same execution number. Also keeps "
             "a first timed run that is more than "
             f"{FIRST_RUN_OUTLIER_PCT:g}%% off the rest of its runs, which is "
             "otherwise replaced by one more run."
    )
    g_runs.add_argument(
        "--warmup-budget",
        dest="warmup_budget",
        type=float,
        default=WARMUP_BUDGET_MS,
        metavar="MS",
        help=f"Wall-clock ceiling (ms) on the untimed warm-up runs of one query "
             f"(default: {WARMUP_BUDGET_MS:.0f}). Ignored with --no-warmup."
    )
    g_runs.add_argument(
        "--runs-exec-budget",
        dest="runs_exec_budget",
        type=float,
        default=RUNS_EXEC_BUDGET_MS,
        metavar="MS",
        help=f"Complementary to --runs-budget: target total PURE QUERY time (ms) -- "
             f"the measured per-run times, eviction excluded -- the dynamic run-count "
             f"spends before stopping (default: {RUNS_EXEC_BUDGET_MS:.0f}). AND'd "
             f"with --runs-budget, so a query that is merely cheap cannot defeat the "
             f"wall budget and run to {RUNS_MAX} however expensive its per-run "
             f"eviction is. Clamped to [{RUNS_MIN}, {RUNS_MAX}] runs. Ignored when "
             f"an exact --runs count is given."
    )

    # ----- measurement method: how each query is timed ----- #
    g_method = parser.add_argument_group(
        "measurement method",
        "How each query is timed. Default: a cursor MOVE FORWARD ALL streams every "
        "tuple through the executor and discards it server-side -- no output "
        "formatting, no result shipping, no client in the timed window.  The "
        "serialize+ship half of an EXECUTE-drain run is per-row backend "
        "infrastructure the patches never touch; on row-heavy queries it both "
        "dilutes scan-side ratios and amplifies binary code-placement stalls by "
        "the row count (A39: 74% of the wall, and a 1.09x 'regression' whose "
        "counters were op-cache misses in unchanged drain code).")
    g_method.add_argument(
        "--serialize",
        action="store_true",
        dest="serialize",
        help="Time the full delivery instead: EXECUTE the prepared generic plan and "
             "let libpq drain and buffer every row (the pre-2026-09-25 default). "
             "Includes output functions, DataRow shipping, and the client's "
             "concurrent drain in the timed wall. get_actual_variable_range-clean "
             "(the prepared statement re-plans nothing). Compare against the "
             "default: a disagreement between the two modes on a cached query is "
             "the layout-stall fingerprint -- relink before reading it as a "
             "regression. --instrument path: adds the SERIALIZE EXPLAIN option."
    )
    g_method.add_argument(
        "--no-serialize",
        action="store_true",
        dest="no_serialize_flag",
        help="(default) Measure execution without output serialization: MOVE "
             "FORWARD ALL on the raw query. NOTE: DECLARE CURSOR has no prepared "
             "form, so this re-plans every run and is not "
             "get_actual_variable_range-clean; the cost is symmetric and "
             "negligible at suite query sizes."
    )
    g_method.add_argument(
        "--cursor",
        action="store_true",
        dest="cursor",
        help="Read the result the way a real application does -- a server-side "
             "cursor iterated in fixed-size batches, materializing every row into "
             "Python objects as it arrives, so client decode overlaps server "
             "execution/prefetch (instead of libpq buffering the whole result "
             "first). Implies --serialize. Re-plans the raw query each call (not "
             "get_actual_variable_range-clean), which is intended: this models a "
             "realistic client, not clean executor timing. Ignored under "
             "--instrument."
    )
    g_method.add_argument(
        "--instrument",
        action="store_true",
        dest="instrument",
        help="Measure with EXPLAIN (ANALYZE, ..., IO) instead of the default "
             "MOVE-and-discard timing. Captures the plan + per-scan IO stats but "
             "adds executor instrumentation overhead that inflates the time (esp. for "
             "index-bound scans); the IO option is skipped on builds whose EXPLAIN "
             "lacks it (e.g. rel18, rel19)."
    )
    g_method.add_argument(
        "--timing",
        action="store_true",
        dest="instrument_timing",
        help="Enable per-node TIMING ON in the EXPLAIN ANALYZE measurement (more "
             "detail, more distortion; default is TIMING OFF). Implies --instrument."
    )
    g_method.add_argument(
        "--timed-instrument",
        action="store_true",
        dest="timed_instrument",
        help="--instrument with per-node TIMING ON and COSTS ON: every node in "
             "the reported plan shows its estimated startup..total cost next to "
             "its actual startup..total wall-clock time, for comparing where "
             "the planner puts startup cost against where startup time really "
             "goes. Measures exactly like --timing (the costs only add plan "
             "text); implies --instrument and --timing."
    )

    # ----- result classification & reporting ----- #
    g_report = parser.add_argument_group("result classification & reporting")
    g_report.add_argument(
        "--noise-abs-ms",
        dest="noise_abs_ms",
        type=float,
        default=NOISE_ABS_MS,
        metavar="MS",
        help=f"Classification noise floor in ms (default {NOISE_ABS_MS:g}): a "
             f"per-query master-vs-patch DIFFERENCE this small is called neutral "
             f"regardless of the %% ratio or the query's runtime, so a regression "
             f"must be slower both by >1%% AND by >this many ms. Acts on the "
             f"delta, not the runtime."
    )
    g_report.add_argument(
        "--median",
        action="store_true",
        dest="use_median",
        help="Use median instead of min as the representative value in reports and comparisons"
    )
    g_report.add_argument(
        "--terse",
        action="store_true",
        help="Suppress per-run times and per-query detail; print only header, verification, and summary"
    )
    g_report.add_argument(
        "--no-confirm", "--no-reconfirm",
        action="store_true",
        dest="no_confirm",
        help="Skip the auto-confirmation pass. By default, queries that come out "
             "as a regression (patch slower than the baseline) are re-measured "
             "on both servers and the new samples pooled (min) into the result, "
             "so a one-off transient that spanned all of a query's runs is "
             "filtered out instead of being reported. Improvements (the expected "
             "direction) are not re-confirmed. Disabled automatically with an "
             "exact --runs count, --old-master-results, or --queries (an explicit "
             "query list is usually a quick targeted check; pass --confirm to "
             "keep the pass)."
    )
    g_report.add_argument(
        "--confirm",
        action="store_true",
        dest="confirm",
        help="Run the auto-confirmation pass even with --queries, which otherwise "
             "skips it."
    )

    # ----- builds compared ----- #
    g_builds = parser.add_argument_group("builds compared")
    g_builds.add_argument(
        "--baseline",
        type=str,
        choices=list(BASELINE_CONFIGS.keys()),
        default="master",
        help="Baseline PostgreSQL build to compare against (default: master). "
             "master runs against the testbranch's own data directory; a back "
             "branch has a dedicated one"
    )
    g_builds.add_argument(
        "--testbranch",
        type=str,
        choices=list(TESTBRANCH_CONFIGS.keys()),
        default="patch",
        help="PostgreSQL build to test (default: patch). Use 'master' to compare "
             "master vs a back branch (rel18, rel19)."
    )
    g_builds.add_argument(
        "--old-master-results",
        action="store_true",
        dest="old_master_results",
        help="Use baseline results from most recent benchmark run instead of re-running baseline"
    )

    # ----- patch prefetch configuration ----- #
    # By default only prefetch=on is tested on the patch.  The prefetch=off
    # comparison is opt-in (--include-no-prefetch for both, --prefetch-disabled
    # for off only).  args.prefetch_only is derived from these after parsing.
    g_prefetch = parser.add_argument_group(
        "patch prefetch configuration",
        "By default only prefetch=on is tested on the patch; these opt into the "
        "prefetch=off comparison.")
    prefetch_group = g_prefetch.add_mutually_exclusive_group()
    prefetch_group.add_argument(
        "--include-no-prefetch",
        action="store_true",
        dest="include_no_prefetch",
        help="Also test the patch with prefetching disabled (run both "
             "prefetch=on and prefetch=off). By default only prefetch=on runs."
    )
    prefetch_group.add_argument(
        "--prefetch-disabled",
        action="store_true",
        dest="prefetch_disabled",
        help="Only test patch with prefetching disabled (skip prefetch=on)"
    )
    prefetch_group.add_argument(
        "--prefetch-only",
        action="store_true",
        dest="prefetch_only_explicit",
        help="Only test patch with prefetching enabled (skip prefetch=off). "
             "This is the default; accepted for backward compatibility."
    )

    # ----- server & I/O configuration ----- #
    g_server = parser.add_argument_group("server & I/O configuration")
    # Server-start io GUC knobs, shared verbatim with perf_flamegraph.
    add_io_server_args(g_server)
    g_server.add_argument(
        "--delay-pgdata",
        action="store_true",
        dest="delay_pgdata",
        help="Use data directories with simulated I/O delay (data-delay instead of data)"
    )
    g_server.add_argument(
        "--no-tmpfs-hugepages",
        action="store_true",
        help="Run the servers from their install directories, instead of from "
             "copies on a tmpfs whose postgres binaries are on 2MB pages (the "
             "default)"
    )
    g_server.add_argument(
        "--global-drop-caches",
        action="store_true",
        dest="global_drop_caches",
        help="Make an uncached run cold the old way: drop the WHOLE system page "
             "cache (sudo clear_cache.sh) before every timed query, instead of "
             "calling posix_fadvise(DONTNEED) on just the relations being "
             "evicted. ~490 ms per run against ~10 ms, for the same queries -- "
             "keep it only to reproduce results taken before the change."
    )

    # ----- system & environment ----- #
    g_system = parser.add_argument_group("system & environment")
    g_system.add_argument(
        "--benchmark-cpu",
        type=int,
        default=BENCHMARK_CPU,
        help=f"CPU core to pin PostgreSQL backend to (default: {BENCHMARK_CPU})"
    )
    g_system.add_argument(
        "--client-cpu",
        type=int,
        default=CLIENT_CPU,
        help=f"CPU core to pin this benchmark process (the client) to when "
             f"pinning is on (default: {CLIENT_CPU}, same L3 domain as the "
             f"default --benchmark-cpu but off the backend's core). Must "
             f"differ from --benchmark-cpu."
    )
    g_system.add_argument(
        "--pin",
        action=argparse.BooleanOptionalAction,
        dest="pin",
        default=None,
        help="Pin the backend to --benchmark-cpu with SCHED_FIFO and this "
             "client process to --client-cpu (--pin), or leave both unpinned "
             "(--no-pin). Default: on with --cached, off otherwise, because a "
             "pinned SCHED_FIFO backend tends to interfere with io_uring on "
             "uncached runs. Either flag wins over the mode's default."
    )
    g_system.add_argument(
        "--skip-load",
        action="store_true",
        dest="skip_load",
        help="Skip data loading verification (assume tables exist with correct data). "
             "This also skips copying a back-branch baseline's optimizer "
             "statistics to the testbranch (sync_stats suites)"
    )
    g_system.add_argument(
        "--no-kill-interferers",
        action="store_true",
        dest="no_kill_interferers",
        help="Skip the kill_vscode_server.sh step entirely."
    )

    # ----- alternate modes ----- #
    g_modes = parser.add_argument_group("alternate modes")
    g_modes.add_argument(
        "--stress-test",
        action="store_true",
        dest="stress_test",
        help="Run stress test mode: randomly generate queries to find regressions"
    )
    g_modes.add_argument(
        "--min-query-ms",
        type=float,
        default=STRESS_MIN_QUERY_MS,
        dest="min_query_ms",
        help=f"Minimum query duration in ms for stress test (default: {STRESS_MIN_QUERY_MS}). "
             "Queries faster than this on the baseline are too noisy to time, and are "
             "replaced by new ones."
    )
    g_modes.add_argument(
        "--stress-avoid-known",
        type=float,
        default=STRESS_AVOID_KNOWN,
        dest="stress_avoid_known",
        metavar="P",
        help=f"Stress test novelty bias (default: {STRESS_AVOID_KNOWN}). A generated "
             "query whose shape a suite already covers (STRESS_KNOWN_BAD) is redrawn "
             "with probability P ** (1 + tags it adds to that shape); 0 turns the "
             f"bias off, 1 redraws every covered shape (keeping the last draw "
             f"after {STRESS_AVOID_MAX_REDRAWS} tries)."
    )
    g_modes.add_argument(
        "--stress-avoid",
        default="",
        dest="stress_avoid",
        metavar="SHAPES",
        help="Extra shapes for the stress test to avoid this run, on top of "
             "STRESS_KNOWN_BAD: comma-separated, tags joined by '+', e.g. "
             "'LATERAL+backwards,range-scan+product-idx'. For a regression a "
             "stress run found but no suite has yet."
    )
    g_modes.add_argument(
        "--list-modes",
        action="store_true",
        dest="list_modes",
        help="Output benchmark suite metadata as JSON (used by orchestration scripts)"
    )
    g_modes.add_argument(
        "--verify",
        action="store_true",
        dest="verify",
        help="Plan-verification mode (no timing): run every selected query ONCE, "
             "cached, on the baseline and on the testbranch, and check that both "
             "produce the same plan shape, the same scan nodes on the same "
             "relations/indexes, the same 'actual rows'/loops and the same 'Rows "
             "Removed by Filter' at every node.  Any disagreement is flagged for "
             "further debugging (exit status 1).  Ignores --cached/--runs and the "
             "timing-method flags; never uses --old-master-results."
    )

    args = parser.parse_args()

    # When driven from inside a Claude Code session (CLAUDECODE is set in the
    # environment of every command it runs), auto-apply two conveniences so we
    # don't have to remember the flags -- and, more importantly, so we never
    # tear down the very session running the benchmark:
    #   * force --no-kill-interferers: kill_vscode_server.sh kills the 'claude'
    #     and 'vscode-server' targets under --force, which would kill us.
    #   * default --skip-load: the agent workflow reuses already-loaded data.
    # No flag undoes either; unset CLAUDECODE (env -u CLAUDECODE) for the
    # normal defaults.
    # The notices go to stderr: --list-modes output is parsed as JSON by
    # patch_report.py, and stdout must stay clean for it.
    if os.environ.get("CLAUDECODE"):
        if not args.no_kill_interferers:
            print("CLAUDECODE detected: forcing --no-kill-interferers so the "
                  "driving claude/vscode session is not killed.", file=sys.stderr)
            args.no_kill_interferers = True
        if not args.skip_load:
            print("CLAUDECODE detected: assuming --skip-load (reusing "
                  "already-loaded data).", file=sys.stderr)
            args.skip_load = True

    # --no-cached-index is a stricter flavor of uncached; a cached run with
    # cold indexes is a contradiction.
    if args.no_cached_index and args.cached:
        parser.error("--no-cached-index cannot be combined with --cached")

    # Pinning the backend (CPU affinity + SCHED_FIFO) is wanted for cached runs,
    # but on uncached runs it tends to interfere with io_uring, so the default
    # follows the mode: on with --cached, off otherwise.  --pin / --no-pin force
    # it either way, independent of the mode.
    args.pin_explicit = args.pin is not None
    if args.pin is None:
        args.pin = args.cached

    # A client pinned onto the backend's CPU would fight the SCHED_FIFO
    # backend for it during every timed drain; refuse rather than adjust.
    if args.pin and args.client_cpu == args.benchmark_cpu:
        parser.error("--client-cpu must differ from --benchmark-cpu")

    # Prefetch=on only is the default; the prefetch=off run is opt-in.
    # --include-no-prefetch runs both; --prefetch-disabled runs off only.
    # Downstream code reads args.prefetch_only / args.prefetch_disabled.
    args.prefetch_only = not (args.include_no_prefetch or args.prefetch_disabled)

    # --serialize and --no-serialize name the two sides of one switch.
    if args.serialize and args.no_serialize_flag:
        parser.error("--serialize and --no-serialize are mutually exclusive")

    # --serialize/--no-serialize apply to the stress test too: a stress
    # regression is only worth chasing if the regular benchmark, measuring
    # the same way, can reproduce it.  The remaining timing-method flags
    # (plan capture / client modeling) and the plan-forcing --bitmap are
    # benchmark-path-only, so reject them there rather than silently
    # ignoring them.
    if args.stress_test:
        bad = [flag for flag, on in (("--cursor", args.cursor),
                                      ("--instrument", args.instrument),
                                      ("--timing", args.instrument_timing),
                                      ("--timed-instrument", args.timed_instrument),
                                      ("--bitmap", args.bitmap))
               if on]
        if bad:
            parser.error(f"--stress-test does not support {', '.join(bad)}")

    # --cursor reads the result set back, which is a serialize-path variant;
    # explicitly refusing serialization contradicts it.
    if args.cursor:
        if args.no_serialize_flag:
            parser.error("--cursor reads the result set; it cannot be combined "
                         "with --no-serialize")
        args.serialize = True

    # --verify checks plans for the fixed suite queries only; the stress test's
    # random queries have no stored expectation to compare.
    if args.stress_test and args.verify:
        parser.error("--verify cannot be combined with --stress-test")

    # The novelty-bias knobs only mean something to the stress test.
    if args.stress_avoid and not args.stress_test:
        parser.error("--stress-avoid requires --stress-test")
    if not 0.0 <= args.stress_avoid_known <= 1.0:
        parser.error("--stress-avoid-known must be between 0 and 1")
    try:
        args.stress_avoid_entries = parse_stress_avoid(args.stress_avoid)
    except ValueError as e:
        parser.error(f"--stress-avoid: {e}")

    if args.runs is not None and args.runs < 1:
        parser.error("--runs must be at least 1")

    # --timed-instrument is --timing plus planner costs in the reported plan:
    # per-node TIMING ON supplies the actual startup..total wall-clock times,
    # COSTS ON puts the estimated startup..total costs beside them.
    if args.timed_instrument:
        args.instrument_timing = True

    # --timing is a detail level of the EXPLAIN ANALYZE path, so it implies
    # --instrument (the path that consumes per-node TIMING).
    if args.instrument_timing:
        args.instrument = True

    if args.direct_io and args.patch_direct:
        parser.error("--patch-direct conflicts with --direct-io "
                     "(--direct-io already runs both servers with direct I/O)")

    # prefetch=off needs the prefetch GUC; without it the testbranch would
    # measure nothing, and --verify would pass having compared nothing.
    if args.prefetch_disabled and not BUILD_HAS_PREFETCH[args.testbranch]:
        parser.error(f"--prefetch-disabled needs a testbranch with the "
                     f"prefetch GUC; '{args.testbranch}' has none")

    return args


# --- Stress-test query generation ---

def random_date_range():
    """Generate a random date range within 2023."""
    # Start date: random day in 2023
    start_day = random.randint(1, 330)  # Leave room for range
    start_date = datetime(2023, 1, 1) + timedelta(days=start_day)

    # Determine range size based on selectivity
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        # Narrow range: 1-3 days (high selectivity)
        days = random.randint(1, 3)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        # Wide range: 30-90 days (low selectivity)
        days = random.randint(30, 90)
    else:
        # Medium range: 5-20 days
        days = random.randint(5, 20)

    end_date = start_date + timedelta(days=days)
    return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')


def random_customer_range():
    """Generate a random customer_id range."""
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        # Single customer or very small range
        start = random.randint(1, 100000)
        count = random.randint(1, 5)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        # Large range: 5000-20000 customers
        start = random.randint(1, 80000)
        count = random.randint(5000, 20000)
    else:
        # Medium range: 100-1000 customers
        start = random.randint(1, 99000)
        count = random.randint(100, 1000)

    end = min(start + count, 100000)
    return start, end


def random_product_range():
    """Generate a random product_id range."""
    if random.random() < STRESS_PROB_HIGH_SELECTIVITY:
        start = random.randint(1, 10000)
        count = random.randint(1, 10)
    elif random.random() < STRESS_PROB_LOW_SELECTIVITY:
        start = random.randint(1, 5000)
        count = random.randint(2000, 5000)
    else:
        start = random.randint(1, 9000)
        count = random.randint(50, 500)

    end = min(start + count, 10000)
    return start, end


def random_in_list(start, end, max_items=10):
    """Generate a random IN list from a range."""
    count = min(random.randint(3, max_items), end - start + 1)
    values = random.sample(range(start, end + 1), count)
    return ', '.join(str(v) for v in sorted(values))


def random_filter_qual(table_alias=""):
    """Generate a random filter qual that can't use the index."""
    prefix = f"{table_alias}." if table_alias else ""
    qual_type = random.choice(['amount_gt', 'amount_between', 'region_in'])

    if qual_type == 'amount_gt':
        threshold = random.randint(100, 900)
        return f"{prefix}amount > {threshold}"
    elif qual_type == 'amount_between':
        low = random.randint(0, 500)
        high = low + random.randint(100, 400)
        return f"{prefix}amount BETWEEN {low} AND {high}"
    else:  # region_in
        regions = random.sample(range(1, 21), random.randint(2, 5))
        return f"{prefix}region_id IN ({', '.join(str(r) for r in regions)})"


def random_limit():
    """Generate a random LIMIT value."""
    if random.random() < 0.3:
        return random.randint(1, 10)  # Very small
    elif random.random() < 0.5:
        return random.randint(100, 1000)  # Medium
    else:
        return random.randint(5000, 50000)  # Large


def _draw_random_query(query_num):
    """
    Draw one random query targeting the prefetch benchmark tables, with no
    novelty bias (generate_random_query() adds that).  Returns a query
    definition dict compatible with the suite query format, plus "features":
    the shape tags that make up the query's name.
    """
    # Decide query type based on probabilities
    # These are mutually exclusive special patterns
    use_lateral = random.random() < STRESS_PROB_LATERAL_JOIN
    use_anti_join = not use_lateral and random.random() < STRESS_PROB_ANTI_JOIN
    use_semi_join = not use_lateral and not use_anti_join and random.random() < STRESS_PROB_SEMI_JOIN
    use_correlated = not use_lateral and not use_anti_join and not use_semi_join and random.random() < STRESS_PROB_CORRELATED_SUBQUERY
    use_self_join = not use_lateral and not use_anti_join and not use_semi_join and not use_correlated and random.random() < STRESS_PROB_SELF_JOIN
    use_aggregate = not use_lateral and not use_self_join and random.random() < STRESS_PROB_AGGREGATE

    # Independent features
    use_filter_qual = random.random() < STRESS_PROB_FILTER_QUAL
    use_order_by = random.random() < STRESS_PROB_ORDER_BY
    use_limit = use_order_by and random.random() < STRESS_PROB_LIMIT
    use_backwards = use_order_by and random.random() < STRESS_PROB_BACKWARDS_SCAN
    use_in_list = random.random() < STRESS_PROB_IN_LIST
    use_index_only = random.random() < STRESS_PROB_INDEX_ONLY
    # A drawn self-join is one of the exclusive patterns above, so the JOIN
    # branch, which the chain below tests first, must not take it over.
    use_multi_join = random.random() < STRESS_PROB_MULTI_TABLE_JOIN and not use_self_join
    use_single_date = random.random() < STRESS_PROB_SINGLE_DATE
    use_force_merge = random.random() < STRESS_PROB_FORCE_MERGE
    use_sort_non_indexed = use_order_by and random.random() < STRESS_PROB_SORT_NON_INDEXED

    # Generate date range (used in most queries)
    date_start, date_end = random_date_range()
    cust_start, cust_end = random_customer_range()
    prod_start, prod_end = random_product_range()

    # Build date condition: single-date equality or BETWEEN range.
    # Single-date creates more zero-row inner scans (only ~0.7 orders/customer/day).
    if use_single_date:
        date_cond = f"order_date = '{date_start}'"
        date_cond_aliased = lambda alias: f"{alias}.order_date = '{date_start}'"
    else:
        date_cond = f"order_date BETWEEN '{date_start}' AND '{date_end}'"
        date_cond_aliased = lambda alias: f"{alias}.order_date BETWEEN '{date_start}' AND '{date_end}'"

    # Build query based on type
    evict = ["prefetch_orders"]
    prewarm_indexes = []
    prewarm_tables = ["prefetch_orders"]
    query_features = []
    # Stress queries are planned with random_page_cost = 1.1.  It is part of
    # the query's gucs, so set_gucs() applies it before every run, and a
    # regression copied into a suite from the printed TOML plans the same way.
    gucs = {"random_page_cost": "1.1"}
    date_cond_used = True

    if use_lateral:
        # LATERAL join: top-N per customer
        query_features.append("LATERAL")
        use_sort_non_indexed = False   # always ORDER BY order_date
        limit_val = random.randint(3, 10)
        order_dir = "DESC" if use_backwards else ""

        if use_in_list and (cust_end - cust_start) <= 20:
            query_features.append("in-list")
            cust_cond = f"c.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"c.customer_id BETWEEN {cust_start} AND {cust_end}"

        inner_filter = ""
        if use_filter_qual:
            # Inside LATERAL subquery, prefetch_orders has no alias, so no prefix needed
            # But avoid region_id which would be ambiguous - use amount only
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                inner_filter = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                inner_filter = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        sql = f"""
            SELECT c.customer_id, o.order_id, o.order_date, o.amount
            FROM prefetch_customers c,
            LATERAL (
                SELECT order_id, order_date, amount
                FROM prefetch_orders
                WHERE customer_id = c.customer_id
                  AND {date_cond}
                  {inner_filter}
                ORDER BY order_date {order_dir}
                LIMIT {limit_val}
            ) o
            WHERE {cust_cond}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

    elif use_anti_join:
        # NOT EXISTS anti-join
        query_features.append("anti-join")

        if use_in_list and (cust_end - cust_start) <= 20:
            query_features.append("in-list")
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        region_id = random.randint(1, 20)

        select_cols = "o.customer_id, o.order_date" if use_index_only else "o.order_id, o.customer_id, o.amount"
        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_cols}
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND {cust_cond}
              AND NOT EXISTS (
                  SELECT 1 FROM prefetch_customers c
                  WHERE c.customer_id = o.customer_id
                    AND c.region_id = {region_id}
              )
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            query_features.append("order-by")
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                query_features.append("limit")
                sql += f"\n            LIMIT {random_limit()}"

    elif use_semi_join:
        # EXISTS semi-join
        query_features.append("semi-join")

        region_id = random.randint(1, 20)

        select_cols = "o.customer_id, o.order_date" if use_index_only else "o.order_id, o.customer_id, o.amount"
        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_cols}
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND EXISTS (
                  SELECT 1 FROM prefetch_customers c
                  WHERE c.customer_id = o.customer_id
                    AND c.region_id = {region_id}
              )
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            query_features.append("order-by")
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                query_features.append("limit")
                sql += f"\n            LIMIT {random_limit()}"

    elif use_correlated:
        # Correlated subquery in SELECT
        query_features.append("correlated")

        if use_in_list and (cust_end - cust_start) <= 20:
            query_features.append("in-list")
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        filter_clause = ""
        if use_filter_qual:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        sql = f"""
            SELECT o.order_id, o.customer_id, o.amount,
                   (SELECT c.customer_name FROM prefetch_customers c
                    WHERE c.customer_id = o.customer_id) as cust_name
            FROM prefetch_orders o
            WHERE {date_cond_aliased('o')}
              AND {cust_cond}
              {filter_clause}
        """
        evict.append("prefetch_customers")
        prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
        prewarm_tables.append("prefetch_customers")

        if use_order_by:
            query_features.append("order-by")
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n            ORDER BY {order_col} {order_dir}"
            if use_limit:
                query_features.append("limit")
                sql += f"\n            LIMIT {random_limit()}"

    elif use_aggregate:
        # Aggregate query
        query_features.append("aggregate")
        use_sort_non_indexed = False   # GROUP BY order_date: only ORDER BY order_date
        agg_type = random.choice(['count', 'sum', 'both'])

        if agg_type == 'count':
            select_clause = "order_date, count(*) as cnt"
        elif agg_type == 'sum':
            select_clause = "order_date, sum(amount) as total"
        else:
            select_clause = "order_date, count(*) as cnt, sum(amount) as total"

        filter_clause = ""
        if use_filter_qual:
            # No table alias in aggregate query, so no prefix needed
            # Avoid region_id ambiguity by only using amount
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                filter_clause = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                filter_clause = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        sql = f"""
            SELECT {select_clause}
            FROM prefetch_orders
            WHERE {date_cond}
              {filter_clause}
            GROUP BY order_date
        """
        prewarm_indexes = ["prefetch_orders_date_idx"]

        if use_order_by:
            query_features.append("order-by")
            order_dir = "DESC" if use_backwards else ""
            # Aggregate GROUP BY order_date: sort-non-indexed not applicable
            sql = sql.rstrip() + f"\n            ORDER BY order_date {order_dir}"

    elif use_multi_join:
        # Multi-table JOIN
        query_features.append("JOIN")
        join_to = random.choice(['customers', 'products', 'both'])
        if join_to != 'both':
            query_features.append(join_to)

        if use_in_list and (cust_end - cust_start) <= 20:
            query_features.append("in-list")
            cust_cond = f"o.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o.customer_id BETWEEN {cust_start} AND {cust_end}"

        filter_clause = ""
        if use_filter_qual:
            filter_clause = f"AND {random_filter_qual('o')}"
            query_features.append("filter")

        if join_to == 'customers':
            sql = f"""
                SELECT o.order_id, c.customer_name, o.amount, o.order_date
                FROM prefetch_orders o
                JOIN prefetch_customers c ON c.customer_id = o.customer_id
                WHERE {cust_cond}
                  AND {date_cond_aliased('o')}
                  {filter_clause}
            """
            evict.append("prefetch_customers")
            prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey"]
            prewarm_tables.append("prefetch_customers")
        elif join_to == 'products':
            sql = f"""
                SELECT o.order_id, p.product_name, o.amount, o.order_date
                FROM prefetch_orders o
                JOIN prefetch_products p ON p.product_id = o.product_id
                WHERE o.product_id BETWEEN {prod_start} AND {prod_end}
                  AND {date_cond_aliased('o')}
                  {filter_clause}
            """
            evict.append("prefetch_products")
            prewarm_indexes = ["prefetch_orders_prod_idx", "prefetch_products_pkey"]
            prewarm_tables.append("prefetch_products")
        else:  # both
            query_features.append("multi-JOIN")
            sql = f"""
                SELECT o.order_id, c.customer_name, p.product_name, o.amount
                FROM prefetch_orders o
                JOIN prefetch_customers c ON c.customer_id = o.customer_id
                JOIN prefetch_products p ON p.product_id = o.product_id
                WHERE {date_cond_aliased('o')}
                  AND {cust_cond}
                  {filter_clause}
            """
            evict.extend(["prefetch_customers", "prefetch_products"])
            prewarm_indexes = ["prefetch_orders_cust_date_idx", "prefetch_customers_pkey", "prefetch_products_pkey"]
            prewarm_tables.extend(["prefetch_customers", "prefetch_products"])

        if use_order_by:
            query_features.append("order-by")
            order_dir = "DESC" if use_backwards else ""
            order_col = "o.amount" if use_sort_non_indexed else "o.order_date"
            sql = sql.rstrip() + f"\n                ORDER BY {order_col} {order_dir}"
            if use_limit:
                query_features.append("limit")
                sql += f"\n                LIMIT {random_limit()}"

        # Force merge join on multi-table JOINs
        if use_force_merge:
            gucs["enable_hashjoin"] = "off"
            gucs["enable_nestloop"] = "off"
            query_features.append("merge-forced")

    elif use_self_join:
        # Self-join on orders: both sides use same index, can trigger
        # MergeJoin mark/restore when outer has duplicate join keys.
        query_features.append("self-join")
        use_backwards = use_sort_non_indexed = False   # no ORDER BY
        date_cond_used = False         # tags "single-date" itself, below

        # Generate two separate date ranges for the two sides
        date_start2, date_end2 = random_date_range()

        if use_in_list and (cust_end - cust_start) <= 20:
            query_features.append("in-list")
            cust_cond = f"o1.customer_id IN ({random_in_list(cust_start, cust_end)})"
        else:
            cust_cond = f"o1.customer_id BETWEEN {cust_start} AND {cust_end}"

        date_cond1 = (f"o1.order_date = '{date_start}'"
                      if use_single_date else
                      f"o1.order_date BETWEEN '{date_start}' AND '{date_end}'")
        date_cond2 = (f"o2.order_date = '{date_start2}'"
                      if use_single_date else
                      f"o2.order_date BETWEEN '{date_start2}' AND '{date_end2}'")
        if use_single_date:
            query_features.append("single-date")

        sql = f"""
            SELECT count(*)
            FROM prefetch_orders o1
            JOIN prefetch_orders o2 ON o1.customer_id = o2.customer_id
            WHERE {cust_cond}
              AND {date_cond1}
              AND {date_cond2}
        """
        prewarm_indexes = ["prefetch_orders_cust_date_idx"]

        # Always force merge join on self-joins — that's the point
        gucs["enable_hashjoin"] = "off"
        gucs["enable_nestloop"] = "off"
        query_features.append("merge-forced")

    else:
        # Simple range scan on prefetch_orders
        query_features.append("range-scan")

        # Choose which index to target
        index_choice = random.choice(['date', 'cust_date', 'product'])
        query_features.append({"date": "date-idx", "cust_date": "cust-date-idx",
                               "product": "product-idx"}[index_choice])

        if use_index_only:
            query_features.append("index-only")

        filter_clause = ""
        if use_filter_qual and not use_index_only:
            # No table alias in simple range scan, avoid region_id ambiguity
            qual_type = random.choice(['amount_gt', 'amount_between'])
            if qual_type == 'amount_gt':
                threshold = random.randint(100, 900)
                filter_clause = f"AND amount > {threshold}"
            else:
                low = random.randint(0, 500)
                high = low + random.randint(100, 400)
                filter_clause = f"AND amount BETWEEN {low} AND {high}"
            query_features.append("filter")

        if index_choice == 'date':
            select_cols = "order_date" if use_index_only else "order_id, customer_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {date_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_date_idx"]

        elif index_choice == 'cust_date':
            if use_in_list and (cust_end - cust_start) <= 20:
                query_features.append("in-list")
                cust_cond = f"customer_id IN ({random_in_list(cust_start, cust_end)})"
            else:
                cust_cond = f"customer_id BETWEEN {cust_start} AND {cust_end}"

            select_cols = "customer_id, order_date" if use_index_only else "order_id, customer_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {cust_cond}
                  AND {date_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_cust_date_idx"]

        else:  # product
            date_cond_used = False
            if use_in_list and (prod_end - prod_start) <= 20:
                query_features.append("in-list")
                prod_cond = f"product_id IN ({random_in_list(prod_start, prod_end)})"
            else:
                prod_cond = f"product_id BETWEEN {prod_start} AND {prod_end}"

            select_cols = "product_id" if use_index_only else "order_id, product_id, amount"
            sql = f"""
                SELECT {select_cols}
                FROM prefetch_orders
                WHERE {prod_cond}
                  {filter_clause}
            """
            prewarm_indexes = ["prefetch_orders_prod_idx"]

        if use_order_by:
            query_features.append("order-by")
            if use_sort_non_indexed:
                order_col = "amount"
            else:
                order_col = "order_date" if index_choice in ['date', 'cust_date'] else "product_id"
            order_dir = "DESC" if use_backwards else ""
            sql = sql.rstrip() + f"\n                ORDER BY {order_col} {order_dir}"
            if use_limit:
                query_features.append("limit")
                sql += f"\n                LIMIT {random_limit()}"

    # Add feature tags for independent modifiers.  A branch that does not
    # emit a modifier clears its use_* flag above, so the tags match the SQL.
    if use_backwards:
        query_features.append("backwards")
    if use_single_date and date_cond_used:
        query_features.append("single-date")
    if use_sort_non_indexed:
        query_features.append("sort-non-indexed")

    # Build name from features
    name = f"Stress #{query_num}: {', '.join(query_features)}"

    result = {
        "name": name,
        "sql": sql,
        "evict": list(set(evict)),  # Remove duplicates
        "prewarm_indexes": prewarm_indexes,
        "prewarm_tables": list(set(prewarm_tables)),
        "features": query_features,
    }
    if gucs:
        result["gucs"] = gucs
    return result


def stress_tags(query_def):
    """The shape tags of a generated query as a frozenset (see STRESS_TAGS)."""
    return frozenset(query_def["features"])


def stress_reject_prob(tags, known, base):
    """Probability that generate_random_query() redraws a candidate with these
    tags.  The closest known shape the tags contain decides: base ** (1 + extra),
    extra being the tags the candidate adds on top of that shape.  0 when the
    tags contain no known shape."""
    best = 0.0
    for shape, _origin in known:
        if shape <= tags:
            best = max(best, base ** (1 + len(tags) - len(shape)))
    return best


def parse_stress_avoid(spec):
    """Parse a --stress-avoid value into STRESS_KNOWN_BAD-style entries:
    comma-separated shapes, each a '+'-joined set of STRESS_TAGS."""
    entries = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        tags = frozenset(t.strip() for t in item.split("+") if t.strip())
        if not tags:
            # An empty shape is a subset of every query's tags: it would
            # bias every draw, not avoid one shape.
            raise ValueError(f"no tags in '{item}'")
        unknown = sorted(tags - STRESS_TAGS)
        if unknown:
            raise ValueError(f"unknown tag(s) {', '.join(unknown)} in '{item}'; "
                             f"tags are {', '.join(sorted(STRESS_TAGS))}")
        entries.append((tags, "--stress-avoid"))
    return entries


def describe_stress_avoid(avoid, base, n_extra):
    """One line for the stress-test header."""
    if base <= 0.0 or not avoid:
        return "off"
    origin = f"{len(avoid) - n_extra} shapes from STRESS_KNOWN_BAD"
    if n_extra:
        origin += f" + {n_extra} from --stress-avoid"
    return (f"redraw a covered shape with p = {base:g} ** (1 + extra tags), "
            f"at most {STRESS_AVOID_MAX_REDRAWS} redraws per slot ({origin})")


def stress_avoid_redraws(stats):
    return sum(n for key, n in stats.items() if key != "capped")


def print_stress_avoid_stats(stats):
    total = stress_avoid_redraws(stats)
    if not total:
        print("Novelty bias: no candidate redrawn")
        return
    capped = stats.get("capped", 0)
    print(f"Novelty bias: {total} candidate(s) redrawn"
          + (f"; {capped} slot(s) kept a covered shape after "
             f"{STRESS_AVOID_MAX_REDRAWS} redraws" if capped else ""))
    by_shape = sorted(((n, key) for key, n in stats.items() if key != "capped"),
                      key=lambda x: -x[0])
    for n, tags in by_shape[:10]:
        print(f"  {n:5d}  {'+'.join(sorted(tags))}")


def generate_random_query(query_num, avoid=(), avoid_base=0.0, stats=None):
    """Generate a random stress query, biased away from the shapes in avoid.

    Candidates come from _draw_random_query(); one whose tags contain a shape
    in avoid is rejected with stress_reject_prob() and redrawn.  After
    STRESS_AVOID_MAX_REDRAWS rejections the last candidate is kept, so a shape
    space that is mostly covered slows generation down but never stalls it.
    stats, when given, is a Counter of rejected candidates keyed by tag set,
    plus "capped" for slots that hit the redraw limit.
    """
    query_def = None
    for _ in range(STRESS_AVOID_MAX_REDRAWS):
        query_def = _draw_random_query(query_num)
        p = stress_reject_prob(stress_tags(query_def), avoid, avoid_base) if avoid else 0.0
        if p <= 0.0 or random.random() >= p:
            return query_def
        if stats is not None:
            stats[stress_tags(query_def)] += 1
    if stats is not None:
        stats["capped"] += 1
    return query_def


def stress_query_failed(e, query_def):
    """Report a stress query that failed on the server, and end the run.
    Only an error of SQLSTATE class 42 (psycopg.ProgrammingError: a syntax
    error, an undefined column, ...) means the generator wrote bad SQL; a
    data error, a lost connection or a crashed backend does not."""
    print(f"ERROR: {e}")
    if isinstance(e, psycopg.ProgrammingError):
        print("\nGenerated invalid SQL! This is a bug in the query generator.")
    print(f"Query: {query_def['sql']}")
    sys.exit(1)


def get_git_hash(source_dir):
    """Get the current git commit hash for a source directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def get_pg_version(conn_details):
    """Get PostgreSQL version string."""
    try:
        conn = psycopg.connect(**conn_details, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
        conn.close()
        return version
    except Exception as e:
        return f"error: {e}"


def print_io_settings(args):
    """Print the PostgreSQL I/O settings that will be used."""
    print(f"\nPostgreSQL I/O settings:")
    print(f"  effective_io_concurrency = {args.effective_io_concurrency}")
    print(f"  io_combine_limit = {args.io_combine_limit} (8kB units)")
    print(f"  io_max_combine_limit = 128 (8kB units, pinned to max)")
    print(f"  io_max_concurrency = {IO_MAX_CONCURRENCY}")
    print(f"  io_max_workers = {args.io_max_workers}")
    print(f"  io_min_workers = {args.io_min_workers}")
    print(f"  io_worker_launch_interval = {args.io_worker_launch_interval} (ms)")
    print(f"  io_method = {args.io_method}")
    if args.direct_io:
        print(f"  debug_io_direct = data")
    elif args.patch_direct:
        print(f"  debug_io_direct = data (testbranch only; baseline buffered)")


def server_direct_io(args, on_patch):
    """Whether the server on the given side runs with debug_io_direct=data.

    --direct-io puts both servers in direct-I/O mode; --patch-direct puts only
    the testbranch server there, leaving the baseline buffered.
    """
    return args.direct_io or (on_patch and args.patch_direct)


def server_running(pg_bin_dir, pg_data_dir):
    """Whether a postmaster is running on pg_data_dir (pg_ctl status exits 0).
    False when pg_bin_dir has no pg_ctl: the missing binary is reported by
    whoever tries to use it next."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    try:
        result = subprocess.run([pg_ctl_path, "status", "-D", pg_data_dir],
                                capture_output=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def ensure_server_stopped(pg_bin_dir, pg_name, pg_data_dir):
    """Stop whatever postmaster is running on pg_data_dir, so that a fresh
    start_server() owns it."""
    if not server_running(pg_bin_dir, pg_data_dir):
        return
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    print(f"{pg_name}: Server is already running. Stopping it...")
    subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=True)
    time.sleep(2)


def print_log_tail(log_file, lines=20):
    """Print the last lines of a server log; pg_ctl's own failure message only
    says to examine the log output."""
    try:
        with open(log_file, errors="replace") as f:
            tail = f.readlines()[-lines:]
    except OSError:
        return
    if tail:
        print(f"Last {len(tail)} lines of {log_file}:")
        for line in tail:
            print(f"  {line.rstrip()}")


# The FATAL message a postmaster prints when its binary cannot open a data
# directory -- a different major version, PG_CONTROL_VERSION or
# CATALOG_VERSION_NO; the DETAIL line that follows names the two versions.
INCOMPATIBLE_DATA_DIR_MSG = "database files are incompatible with server"


def preflight_data_dir(pg_bin_dir, pg_name, pg_data_dir):
    """Check that the server binary in pg_bin_dir can open pg_data_dir, and
    exit with a clear message if it cannot.  Meant to run before anything else
    touches the machine (tmpfs setup, killing interferers, data loading, the
    first server start).

    'postgres -C <runtime-computed GUC>' runs postmaster startup only as far
    as reading the control file -- exactly where a data directory of a
    different major version, PG_CONTROL_VERSION or CATALOG_VERSION_NO is
    rejected -- then exits.  No server starts, no recovery runs, and nothing
    in the data directory changes beyond the lock file taken and released.
    It takes milliseconds.

    A mismatch means one side is behind the other, and the message says
    which: a binary older than the data directory is a stale install
    (typically master pulled past a catversion bump but never rebuilt and
    reinstalled), a data directory older than the binary needs recreating
    (a delay cluster left behind by a catversion bump, say).  With master
    sharing the patch build's data directory, the stale master install is the
    failure to catch up front; it would otherwise surface only at master's
    first start, after the tmpfs setup and a possible data load.
    """
    postgres_path = os.path.join(pg_bin_dir, "postgres")
    if not os.path.isfile(postgres_path):
        output = f"{postgres_path}: no such file"
    elif server_running(pg_bin_dir, pg_data_dir):
        # The probe needs the lock file.  start_server() stops the server,
        # and its own start then applies the same checks.
        print(f"{pg_name}: a server is running on {pg_data_dir}; the "
              f"binary/data-directory check happens when it is restarted")
        return
    else:
        result = subprocess.run(
            [postgres_path, "-D", pg_data_dir, "-C", "data_checksums"],
            capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        output = (result.stdout + result.stderr).strip()
    line = "!" * 72
    print(f"\n{line}")
    print(f"!! ERROR: the {pg_name} server binary cannot open its data directory")
    print(f"!!   binary:   {postgres_path}")
    print(f"!!   data dir: {pg_data_dir}")
    if INCOMPATIBLE_DATA_DIR_MSG in output:
        detail = re.search(r"DETAIL:\s*(.*)", output)
        detail = detail.group(1).strip() if detail else ""
        print(f"!!   {detail}")
        # Which side is behind?  The DETAIL names the two version numbers.
        versions = re.search(r"initialized with (\w+) (\d+), but the server "
                             r"was compiled with \1 (\d+)", detail)
        if versions and int(versions.group(3)) < int(versions.group(2)):
            print("!! The binary is older than the data directory: the "
                  "install is probably stale.  Rebuild and reinstall this "
                  "build, then retry.")
        elif versions:
            print("!! The data directory is older than the binary: it needs "
                  "to be recreated (or reloaded from a build that matches it).")
        else:
            print("!! This build and data directory do not belong together "
                  "(a different major version cannot open the cluster).")
    else:
        for out_line in output.splitlines():
            print(f"!!   {out_line}")
    print(f"{line}\n")
    sys.exit(1)


def preflight_servers(args):
    """preflight_data_dir() for the baseline and testbranch servers a run will
    start.  The baseline is skipped under --old-master-results, which starts
    it only when no saved results match (run_generic_benchmark() checks it
    then); the stress test and --verify ignore that flag and always start it."""
    checks = []
    if not (args.old_master_results and not args.stress_test and not args.verify):
        bin_dir, data_dir, _source_dir, _conn = BASELINE_CONFIGS[args.baseline]
        checks.append((args.baseline, bin_dir, data_dir))
    bin_dir, data_dir, _source_dir, _conn = TESTBRANCH_CONFIGS[args.testbranch]
    checks.append((args.testbranch, bin_dir, data_dir))
    for name, bin_dir, data_dir in checks:
        preflight_data_dir(bin_dir, name, data_dir)


def start_server(pg_bin_dir, pg_name, pg_data_dir, conn_details, args, on_patch):
    """Start a PostgreSQL server and wait for it to be ready."""
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    # pg_ctl -l cannot create a directory, and not every caller has made it.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_file = os.path.join(OUTPUT_DIR, f"{pg_name}.postgres_log")

    ensure_server_stopped(pg_bin_dir, pg_name, pg_data_dir)

    # Start the server
    print(f"Starting {pg_name} PostgreSQL server (port {conn_details.get('port', 'default')})...")
    start_options = f"-p {conn_details['port']}" if 'port' in conn_details else ""

    # Build PostgreSQL configuration options
    pg_options = [
        "--autovacuum=off",
        "-c wal_level=minimal",
        "-c max_wal_senders=0",
        "-c max_parallel_workers_per_gather=0",
        # The benchmark only ever uses a couple of connections. A low
        # max_connections keeps MaxBackends small, which (a) lets io_method=io_uring
        # fit its per-backend rings under RLIMIT_MEMLOCK even at high
        # io_max_concurrency, and (b) shrinks shared memory / huge-page footprint.
        # Keep this value in sync with set-pg-hugepages.sh (the huge-page
        # reservation).
        "-c max_connections=20",
        f"-c effective_io_concurrency={args.effective_io_concurrency}",
        f"-c io_combine_limit={args.io_combine_limit}",
        # Pin the server-wide ceiling to its maximum and rely purely on the
        # per-backend io_combine_limit; we don't care about server-wide limits here.
        "-c io_max_combine_limit=128",
        f"-c io_max_concurrency={IO_MAX_CONCURRENCY}",
        f"-c io_method={args.io_method}",
    ]

    # Detect whether this binary uses the old io_workers GUC or the new
    # io_min_workers/io_max_workers pair (changed in commit d1c01b79d).
    postgres_path = os.path.join(pg_bin_dir, "postgres")
    probe = subprocess.run([postgres_path, "--describe-config"],
                           capture_output=True, text=True, check=False)
    if "io_max_workers" in probe.stdout:
        pg_options.append(f"-c io_max_workers={args.io_max_workers}")
        pg_options.append(f"-c io_min_workers={args.io_min_workers}")
    elif "io_workers" in probe.stdout:
        pg_options.append(f"-c io_workers={args.io_max_workers}")
    # Worker-only; harmless (inert) under io_uring/sync.
    if "io_worker_launch_interval" in probe.stdout:
        pg_options.append(f"-c io_worker_launch_interval={args.io_worker_launch_interval}")
    if server_direct_io(args, on_patch):
        pg_options.append("-c debug_io_direct=data")

    # Launch under the process's original affinity mask: when the client is
    # pinned (pin_client()), pg_ctl would otherwise hand that one-CPU mask
    # down to the postmaster and every child (backends, io workers).
    cmd = ["taskset", "-c", ",".join(str(c) for c in sorted(_ORIG_AFFINITY)),
           pg_ctl_path, "start", "-D", pg_data_dir, "-l", log_file]
    for opt in pg_options:
        cmd.extend(["-o", opt])
    if start_options:
        cmd.extend(["-o", start_options])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: Failed to start {pg_name} server")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        print_log_tail(log_file)
        sys.exit(1)

    # Wait for the server to be ready
    for attempt in range(15):
        try:
            conn = psycopg.connect(**conn_details, connect_timeout=2)
            conn.close()
            return
        except (psycopg.OperationalError, psycopg.DatabaseError):
            if attempt == 14:
                print(f"Error: {pg_name} server failed to start after 15 attempts")
                sys.exit(1)
            time.sleep(0.5 if attempt < 5 else 1)


def stop_server(pg_bin_dir, pg_data_dir):
    """Stop a PostgreSQL server."""
    reset_os_evict_state()
    pg_ctl_path = os.path.join(pg_bin_dir, "pg_ctl")
    subprocess.run([pg_ctl_path, "stop", "-D", pg_data_dir, "-m", "fast"], check=False)


# Relations to sync statistics for
STATS_RELATIONS = [
    # Tables
    'prefetch_orders', 'prefetch_customers', 'prefetch_products',
    'prefetch_sequential', 'prefetch_sparse',
    # Indexes
    'prefetch_orders_cust_date_idx', 'prefetch_orders_date_idx',
    'prefetch_orders_prod_idx', 'prefetch_orders_id_idx',
    'prefetch_sequential_idx', 'prefetch_sparse_cat_idx',
    'prefetch_customers_pkey', 'prefetch_products_pkey',
]


def extract_statistics(conn_details):
    """
    Extract optimizer statistics from a PostgreSQL database.
    Returns a dictionary containing relation and attribute stats.
    """
    conn = psycopg.connect(**conn_details)
    stats = {
        'relation_stats': [],
        'attribute_stats': []
    }

    # Extract relation-level stats from pg_class
    with conn.cursor() as cur:
        for relname in STATS_RELATIONS:
            cur.execute("""
                SELECT c.relname, n.nspname,
                       c.relpages, c.reltuples,
                       c.relallvisible, c.relallfrozen
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = %s AND n.nspname = 'public'
            """, (relname,))
            row = cur.fetchone()
            if row:
                stats['relation_stats'].append({
                    'relname': row[0],
                    'schemaname': row[1],
                    'relpages': row[2],
                    'reltuples': row[3],
                    'relallvisible': row[4],
                    'relallfrozen': row[5],
                })

    # Extract attribute-level stats from pg_stats
    with conn.cursor() as cur:
        for relname in STATS_RELATIONS:
            cur.execute("""
                SELECT schemaname, tablename, attname, inherited,
                       null_frac, avg_width, n_distinct,
                       most_common_vals::text, most_common_freqs,
                       histogram_bounds::text, correlation,
                       most_common_elems::text, most_common_elem_freqs,
                       elem_count_histogram
                FROM pg_stats
                WHERE tablename = %s AND schemaname = 'public'
            """, (relname,))
            for row in cur.fetchall():
                stats['attribute_stats'].append({
                    'schemaname': row[0],
                    'tablename': row[1],
                    'attname': row[2],
                    'inherited': row[3],
                    'null_frac': row[4],
                    'avg_width': row[5],
                    'n_distinct': row[6],
                    'most_common_vals': row[7],
                    'most_common_freqs': list(row[8]) if row[8] else None,
                    'histogram_bounds': row[9],
                    'correlation': row[10],
                    'most_common_elems': row[11],
                    'most_common_elem_freqs': list(row[12]) if row[12] else None,
                    'elem_count_histogram': list(row[13]) if row[13] else None,
                })

    conn.close()
    print(f"Extracted stats for {len(stats['relation_stats'])} relations, "
          f"{len(stats['attribute_stats'])} attributes")
    return stats


def _sql_literal(value, type_cast=None):
    """Convert a Python value to a SQL literal string."""
    if value is None:
        return 'NULL' + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, bool):
        return ('true' if value else 'false') + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, (int, float)):
        return str(value) + (f'::{type_cast}' if type_cast else '')
    elif isinstance(value, list):
        # Format as PostgreSQL array literal
        elements = ', '.join(str(v) for v in value)
        return f"ARRAY[{elements}]" + (f'::{type_cast}' if type_cast else '')
    else:
        # String - escape single quotes
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'" + (f'::{type_cast}' if type_cast else '')


def restore_statistics(conn_details, stats):
    """
    Restore optimizer statistics to a PostgreSQL database.
    Uses pg_restore_relation_stats and pg_restore_attribute_stats.
    """
    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    restored_rels = 0
    restored_attrs = 0

    # Restore relation-level stats
    with conn.cursor() as cur:
        for rs in stats['relation_stats']:
            try:
                sql = f"""
                    SELECT pg_restore_relation_stats(
                        'schemaname', {_sql_literal(rs['schemaname'])},
                        'relname', {_sql_literal(rs['relname'])},
                        'relpages', {_sql_literal(rs['relpages'], 'integer')},
                        'reltuples', {_sql_literal(rs['reltuples'], 'real')},
                        'relallvisible', {_sql_literal(rs['relallvisible'], 'integer')},
                        'relallfrozen', {_sql_literal(rs['relallfrozen'], 'integer')}
                    )
                """
                cur.execute(sql)
                restored_rels += 1
            except Exception as e:
                print(f"Warning: Failed to restore relation stats for {rs['relname']}: {e}")

    # Restore attribute-level stats
    with conn.cursor() as cur:
        for ats in stats['attribute_stats']:
            try:
                sql = f"""
                    SELECT pg_restore_attribute_stats(
                        'schemaname', {_sql_literal(ats['schemaname'])},
                        'relname', {_sql_literal(ats['tablename'])},
                        'attname', {_sql_literal(ats['attname'])},
                        'inherited', {_sql_literal(ats['inherited'], 'boolean')},
                        'null_frac', {_sql_literal(ats['null_frac'], 'real')},
                        'avg_width', {_sql_literal(ats['avg_width'], 'integer')},
                        'n_distinct', {_sql_literal(ats['n_distinct'], 'real')},
                        'most_common_vals', {_sql_literal(ats['most_common_vals'], 'text')},
                        'most_common_freqs', {_sql_literal(ats['most_common_freqs'], 'real[]')},
                        'histogram_bounds', {_sql_literal(ats['histogram_bounds'], 'text')},
                        'correlation', {_sql_literal(ats['correlation'], 'real')},
                        'most_common_elems', {_sql_literal(ats['most_common_elems'], 'text')},
                        'most_common_elem_freqs', {_sql_literal(ats['most_common_elem_freqs'], 'real[]')},
                        'elem_count_histogram', {_sql_literal(ats['elem_count_histogram'], 'real[]')}
                    )
                """
                cur.execute(sql)
                restored_attrs += 1
            except Exception as e:
                print(f"Warning: Failed to restore attribute stats for "
                      f"{ats['tablename']}.{ats['attname']}: {e}")

    conn.close()
    print(f"Restored stats for {restored_rels} relations, {restored_attrs} attributes")


def extract_execution_time(explain_output):
    """Extract execution time from EXPLAIN ANALYZE output."""
    for line in explain_output:
        match = re.search(r'Execution Time: ([\d.]+) ms', line[0])
        if match:
            return float(match.group(1))
    return None


def timing_method_label(args):
    """One word for how the timed runs measure a query, for run headers and
    result files.  Distinct labels keep results taken under different methods
    from ever being compared as if they were the same measurement."""
    if args.instrument:
        return ("explain-serialize" if args.serialize else "explain") + \
            ("-timing" if args.instrument_timing else "")
    if args.cursor:
        return "cursor"
    if args.serialize:
        return "execute-drain"
    return "move"


def pinning_summary(args):
    """One line for the run header: whether the backend gets pinned, and why."""
    state = (f"on (backend CPU {args.benchmark_cpu} + SCHED_FIFO, "
             f"client CPU {args.client_cpu})" if args.pin else "off")
    why = ("--pin" if args.pin and args.pin_explicit
           else "--no-pin" if args.pin_explicit
           else "default for cached" if args.cached
           else "default for uncached")
    return f"{state} [{why}]"


def pin_client(args):
    """Pin this process -- the benchmark client -- to args.client_cpu.

    The client is inside the timed window whenever rows come back to it
    (--serialize, --cursor), and it runs every prewarm and setup statement;
    unpinned, the scheduler places it anew each invocation.  Servers are
    immune to inheriting the mask: start_server() launches pg_ctl under the
    process's original affinity (_ORIG_AFFINITY).
    """
    if not args.pin:
        return
    try:
        os.sched_setaffinity(0, {args.client_cpu})
        print(f"Pinned benchmark client (PID {os.getpid()}) to CPU {args.client_cpu}")
    except OSError as e:
        print(f"Warning: could not pin the benchmark client: {e}")


def pin_backend(pid, cpu, enabled=False):
    """Pin a backend process to a specific CPU."""
    if not enabled:
        return
    try:
        result = subprocess.run(
            ["taskset", "-cp", str(cpu), str(pid)],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            print(f"Pinned backend PID {pid} to CPU {cpu}")

        # Try RT scheduling
        result = subprocess.run(
            ["sudo", "chrt", "-f", "-p", "1", str(pid)],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            print(f"Set backend PID {pid} to SCHED_FIFO")
    except Exception as e:
        print(f"Warning: Could not pin backend: {e}")


PREPARED_STMT_NAME = "_bench_stmt"

# Name of the prepared statement currently set up by prepare_query().  Only one
# benchmark statement is ever prepared at a time (prepare -> run -> deallocate),
# so run_query()/deallocate_query() act on whichever statement is active here.
# prepare_query() can give it a descriptive, query-specific name so the EXECUTE
# (and thus the backend's debug_query_string) identifies which benchmark query
# is running -- handy for tying server-side log output back to a specific query.
_active_stmt_name = PREPARED_STMT_NAME


def bench_stmt_name(query_id):
    """Build a descriptive, SQL-safe prepared-statement name for a query."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(query_id))
    return f"{PREPARED_STMT_NAME}_{safe}"


def bench_connect(conn_details):
    """Open a connection for prepare_query()/run_query().

    psycopg's own statement preparation is turned off.  Once psycopg has
    prepared a statement of its own -- any statement run 5 times, such as
    set_gucs()'s SETs -- Connection.rollback() sends DEALLOCATE ALL, which
    also drops the statement prepare_query() made.  The error handlers that
    roll back to warn and carry on would then fail the query's next EXECUTE.
    """
    return psycopg.connect(**conn_details, prepare_threshold=None)


def prepare_query(conn, sql, gucs=None, is_master=False, prefetch_setting=None,
                  stmt_name=None):
    """Prepare a query so that subsequent runs avoid planning overhead.

    PostgreSQL plans a prepared statement at its first EXECUTE, not at
    PREPARE, and that EXECUTE would be a timed run.  So build the generic
    plan here with an untimed EXPLAIN, with the planning-relevant GUCs
    active; every EXECUTE then reuses the cached plan.  We also force generic
    plan mode so EXECUTE reuses the cached plan without per-execution plan
    cache overhead.

    stmt_name optionally gives the statement a descriptive name (e.g. derived
    from the query id via bench_stmt_name); run_query()/deallocate_query() then
    operate on that same name.

    Returns that EXPLAIN's text: the plan every run of the statement uses.
    """
    global _active_stmt_name
    _active_stmt_name = stmt_name or PREPARED_STMT_NAME
    set_gucs(conn, gucs or {}, is_master=is_master, prefetch_setting=prefetch_setting)
    with conn.cursor() as cur:
        cur.execute("SET plan_cache_mode = force_generic_plan")
        cur.execute(f"PREPARE {_active_stmt_name} AS {sql.strip()}")
        cur.execute(f"EXPLAIN (COSTS OFF) EXECUTE {_active_stmt_name}")
        return "\n".join(row[0] for row in cur.fetchall())


def deallocate_query(conn):
    """Deallocate the prepared benchmark statement."""
    with conn.cursor() as cur:
        cur.execute(f"DEALLOCATE {_active_stmt_name}")


def explain_supports_io(conn_details):
    """Return True if this server's EXPLAIN accepts the IO option.

    The IO option (per-scan I/O instrumentation) comes from the index-prefetch
    work and the master dev tree; a stock back branch (rel18, rel19) rejects it as an
    unrecognized EXPLAIN option.  Probe on a throwaway connection so a rejection
    can't leave the benchmark connection in an aborted-transaction state.
    """
    try:
        probe = psycopg.connect(**conn_details, connect_timeout=5)
    except psycopg.Error:
        return False
    try:
        probe.autocommit = True
        with probe.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, IO, TIMING OFF, COSTS OFF) SELECT 1")
            cur.fetchall()
        return True
    except psycopg.Error:
        return False
    finally:
        probe.close()


# Cursor name for the default discard path (MOVE FORWARD ALL).
_DISCARD_CURSOR = "_bench_discard_cur"

# Server-side cursor name + batch size for --cursor mode's realistic-app read: the
# result is iterated in fixed-size fetches.  1000 rows/fetch is a typical
# application batch (psycopg's own default is 100; ORMs commonly use ~1000-2000).
_APP_CURSOR = "_bench_cursor_read"
CURSOR_FETCH_ROWS = 1000


def time_discard(conn, sql, serialize, use_cursor=False):
    """Execute the prepared benchmark statement server-side, discard the result,
    and return the client wall-clock time in ms -- with NO executor
    instrumentation (unlike EXPLAIN ANALYZE, whose per-node wrapper inflates
    index-bound scans by several %).

    serialize=True, use_cursor=False : EXECUTE the prepared statement
        (_active_stmt_name) and discard the rows.  Because the statement was prepared
        once under force_generic_plan (prepare_query), each EXECUTE reuses the cached
        generic plan and does NOT re-plan -- so no planning overhead, and in
        particular no get_actual_variable_range index probes that would otherwise
        have us time 2-3 index scans instead of one.  The server still serializes
        every row (via the type output functions) and ships the DataRows over the
        connection (a local Unix socket); libpq drains and buffers them all before
        execute() returns.  We never fetch, so zero Python row objects are built (no
        per-row client noise).
    serialize=True, use_cursor=True : model a real application reading the result
        set -- a server-side cursor iterated in fixed-size fetches
        (CURSOR_FETCH_ROWS rows per round trip), building (and here discarding)
        each row as it arrives.  This overlaps client-side row materialization with
        server execution + prefetch the way an app streaming a large result does,
        rather than the fetchall() shape where libpq buffers the ENTIRE result before
        the first Python row is built (server runs to completion, THEN the client
        decodes -- zero overlap, just a constant decode added to both arms).  A
        server-side cursor cannot DECLARE ... FOR EXECUTE, so this runs the raw *sql*
        and re-plans each call (NOT get_actual_variable_range-clean) -- intended
        here: --cursor models a realistic client, not clean executor timing.
    serialize=False: a NO SCROLL cursor + MOVE FORWARD ALL on the raw *sql* --
        streams every tuple through the executor and discards it without output
        formatting or a tuplestore, approximating EXPLAIN (ANALYZE) without
        SERIALIZE.  NOTE: DECLARE CURSOR re-plans on every call (there is no
        DECLARE ... CURSOR FOR EXECUTE), so unlike the EXECUTE path this re-runs
        planning and is NOT get_actual_variable_range-clean -- a niche diagnostic,
        don't trust it for range-predicate queries.  (use_cursor is irrelevant
        here, since nothing is serialized or returned to the client.)
    """
    if serialize and not use_cursor:
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(f"EXECUTE {_active_stmt_name}", prepare=False)
            return (time.perf_counter() - t0) * 1000.0
    if serialize and use_cursor:
        # Realistic-app read: server-side cursor pulled in fixed-size batches,
        # materializing each row as it arrives (see the docstring).  Time the
        # whole client-perceived read: the cursor execute (DECLARE + first fetch,
        # re-plan included) through the end of iteration.
        s = sql.strip().rstrip(";")
        def _read_all():
            with conn.cursor(name=_APP_CURSOR) as cur:
                cur.itersize = CURSOR_FETCH_ROWS
                t0 = time.perf_counter()
                cur.execute(s)
                for _row in cur:
                    pass
                return (time.perf_counter() - t0) * 1000.0
        # A server-side cursor needs a transaction block; open one only in
        # autocommit, otherwise reuse the in-progress tx (as the MOVE path).
        if conn.autocommit:
            with conn.transaction():
                return _read_all()
        return _read_all()
    # No-serialize: cursors require a transaction.  Open one only when the
    # connection is in autocommit mode; otherwise reuse the in-progress one.
    sql = sql.strip().rstrip(";")
    own_tx = conn.autocommit
    with conn.cursor() as cur:
        if own_tx:
            cur.execute("BEGIN")
        cur.execute(f"DECLARE {_DISCARD_CURSOR} NO SCROLL CURSOR FOR {sql}")
        t0 = time.perf_counter()
        cur.execute(f"MOVE FORWARD ALL IN {_DISCARD_CURSOR}")
        dt = (time.perf_counter() - t0) * 1000.0
        cur.execute(f"CLOSE {_DISCARD_CURSOR}")
        if own_tx:
            cur.execute("COMMIT")
    return dt


# --no-cached-index: the uncached workload evicts each query's indexes, from
# shared buffers and the OS page cache both, instead of prewarming them.  Set
# from args in main(), like GLOBAL_DROP_CACHES.
NO_CACHED_INDEX = False


def prepare_cache(conn, query_def, cached_mode, direct_io):
    """Put the caches in the state a timed run of query_def expects."""
    indexes = query_def.get("prewarm_indexes", [])
    if query_def.get("index_only", False):
        if NO_CACHED_INDEX:
            # Pure index-only scan under --no-cached-index: the index is the
            # one relation the scan reads, so it is exactly what must be cold.
            # The heap is never read and can stay as it is; only its
            # visibility map matters, and that stays warm as always.
            evict_relations(conn, indexes)
            prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
            if not direct_io:
                evict_os_cache(conn, indexes)
            return
        # Pure index-only scan: reads only the index + visibility map, never the
        # heap (the table is all-visible).  Warm the index + heap VM and skip heap
        # eviction and the OS-cache drop entirely -- evicting a large heap the scan
        # never reads is wasted work, and skipping it makes the query behave
        # identically cached and uncached (which is correct for index-only scans).
        prewarm_relations(conn, indexes)
        prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
    elif cached_mode:
        # Prewarm everything
        prewarm_relations(conn, indexes)
        prewarm_relations(conn, query_def.get("prewarm_tables", []), include_vm=True)
    else:
        # Uncached: evict heap, prewarm indexes + heap VM, drop the heap's OS
        # cache.  --no-cached-index evicts the indexes alongside the heap
        # instead of prewarming them; the VM stays warm either way.
        evict = list(query_def.get("evict", []))
        if NO_CACHED_INDEX:
            evict += indexes
        evict_relations(conn, evict)
        if not NO_CACHED_INDEX:
            prewarm_relations(conn, indexes)
        prewarm_relations(conn, query_def.get("prewarm_tables", []), vm_only=True)
        if not direct_io:
            # Last, so the prewarms above cannot pull the heap back in behind us.
            evict_os_cache(conn, evict)


def run_query(conn, query_def, cached_mode, is_master, prefetch_setting, benchmark_cpu,
              serialize, direct_io=False, skip_prewarm=False,
              instrument=False, instrument_timing=False, instrument_costs=False,
              io_supported=False, use_cursor=False):
    """
    Run a single query with proper cache preparation and return
    (execution_time_ms, explain_output_or_None).

    Default (instrument=False): time via time_discard() -- MOVE FORWARD ALL
    (serialize=False, the benchmark default) or EXECUTE the prepared statement
    and discard (serialize=True, --serialize) -- with no EXPLAIN ANALYZE
    instrumentation; explain_output is None.  use_cursor=True reads the rows
    back via a server-side cursor to include client-side row decode.

    instrument=True: measure with EXPLAIN (ANALYZE, ...) EXECUTE
    (server-reported Execution Time), adding SERIALIZE only under --serialize,
    the IO option when io_supported, per-node TIMING when instrument_timing,
    and the planner's cost estimates when instrument_costs (COSTS OFF
    otherwise).  The plan text is returned as explain_output.

    serialize has no default on purpose: every caller -- the benchmark path
    and the stress test alike -- passes args.serialize, so a query is timed
    the same way whichever mode runs it, and a new call site cannot silently
    pick its own timing method.

    skip_prewarm: If True, skip cache preparation (prepare_cache()) entirely.
    For the queries that prepare it only once -- cached mode, index_only --
    the caller prepares it before the first run and passes True on every run,
    so the preparation stays out of whatever the caller times around this.
    """
    if not skip_prewarm:
        prepare_cache(conn, query_def, cached_mode, direct_io)

    # Set GUCs
    gucs = query_def.get("gucs", {})
    set_gucs(conn, gucs, is_master=is_master, prefetch_setting=prefetch_setting)
    sql = query_def["sql"]

    # Special handling for warmup queries (warmup_query = true): run twice,
    # untimed, via the same path that will be measured.
    if query_def.get("warmup_query") and not cached_mode:
        if instrument:
            with conn.cursor() as cur:
                cur.execute(f"EXECUTE {_active_stmt_name}")
                cur.execute(f"EXECUTE {_active_stmt_name}")
        else:
            time_discard(conn, sql, serialize, use_cursor)
            time_discard(conn, sql, serialize, use_cursor)

    explain_output = None
    if instrument:
        # EXPLAIN ANALYZE path (opt-in; adds instrumentation overhead).  The IO
        # option is added only on builds whose EXPLAIN accepts it; per-node TIMING
        # is off unless --timing / --timed-instrument (it roughly doubles the
        # distortion).  COSTS is on only under --timed-instrument: the estimates
        # cost nothing at run time but would clutter every other plan report.
        serialize_opt = ", SERIALIZE" if serialize else ""
        io_opt = ", IO" if io_supported else ""
        timing = "ON" if instrument_timing else "OFF"
        costs = "ON" if instrument_costs else "OFF"
        explain_sql = (f"EXPLAIN (ANALYZE, COSTS {costs}, TIMING {timing}"
                       f"{serialize_opt}{io_opt}) EXECUTE {_active_stmt_name}")
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            result = cur.fetchall()
        explain_output = "\n".join(row[0] for row in result)
        exec_time = extract_execution_time(result)
    else:
        # Default: instrumentation-free COPY / MOVE timing.
        exec_time = time_discard(conn, sql, serialize, use_cursor)

    # Reset query-specific GUCs
    if gucs:
        reset_gucs(conn, gucs)

    if exec_time is None:
        print("Warning: Could not extract execution time from EXPLAIN output")
        return None, explain_output

    return exec_time, explain_output


def measure_query(conn, query_def, args, is_master, prefetch_setting, io_supported,
                  on_patch, force_count=None, force_warmup=None):
    """Run a query and return (times, explains, warmup_runs), choosing the run
    count dynamically.

    force_count, when given, runs exactly that many times regardless of --runs or
    the dynamic budget.  The patch phase passes the baseline's run count here so
    both sides get the same number of samples (a slower config would otherwise
    get fewer dynamic runs, biasing its min upward).

    force_warmup, when given, spends exactly that many untimed runs instead of
    letting warm_up() decide.  The patch phase passes the baseline's warm-up run
    count (the warmup_runs this function returns) so both sides take their timed
    samples from the same execution numbers: whether warm_up() runs at all
    depends on the first run's time, and a query whose first run straddles
    WARMUP_MAX_QUERY_MS would otherwise be warmed up on one side only.

    With an explicit --runs/--force-runs N: run exactly N times.  Otherwise run
    RUNS_MIN times, then keep running while BOTH budgets are unmet (capped at
    RUNS_MAX): the AGGREGATE per-run WALL time -- cache eviction + prewarm + the
    timed query -- is below args.runs_budget, AND the aggregate pure QUERY time
    (the measured per-run times, eviction excluded) is below args.runs_exec_budget.
    Whichever budget binds first stops the loop.  The wall budget keeps
    eviction-heavy queries from running dozens of times; the execution budget stops
    a query that is merely cheap, whose per-run wall is almost all setup, from
    buying runs that its min converged past long ago.  Per-run query times are the
    benchmark result (printed unless --terse); wall time only sizes the loop.

    Before any of that, warm_up() spends untimed runs until the query stops
    moving, so the timed runs measure steady state instead of a warm-up climb.
    It costs nothing for a slow query: the first run doubles as the probe and is
    kept as a timed sample when the query turns out to be expensive.

    Cached mode and index_only queries prewarm once, before the first run and
    outside the wall clock: that preparation is not a per-run cost, and a single
    prewarm of a large heap (Q1's takes ~1.7 s) would exhaust the wall budget
    by itself and hold a query whose runs are cheap at the RUNS_MIN floor.
    Plain uncached queries re-evict + re-prewarm on every run, inside the wall.
    Under --no-cached-index an index_only query's index must be re-evicted for
    every run too, so those queries lose the prepare-once shortcut.
    """
    times, explains, walls = [], [], []
    index_only = query_def.get("index_only", False)
    prepare_once = args.cached or (index_only and not args.no_cached_index)
    direct_io = server_direct_io(args, on_patch)
    n_done = 0

    def _run():
        """One full run: cache preparation plus the timed query.  Records
        nothing -- the caller decides whether the sample counts."""
        nonlocal n_done
        if prepare_once and n_done == 0:
            prepare_cache(conn, query_def, args.cached, direct_io)
        t0 = time.perf_counter()
        t, e = run_query(
            conn, query_def, args.cached, is_master=is_master,
            prefetch_setting=prefetch_setting, benchmark_cpu=args.benchmark_cpu,
            serialize=args.serialize, direct_io=direct_io,
            skip_prewarm=prepare_once, instrument=args.instrument,
            instrument_timing=args.instrument_timing,
            instrument_costs=args.timed_instrument,
            io_supported=io_supported, use_cursor=args.cursor)
        wall = (time.perf_counter() - t0) * 1000.0   # full per-run wall: setup + query
        n_done += 1
        return t, e, wall

    def _keep(t, e, wall):
        if t is not None:
            times.append(t)
            explains.append(e)
            walls.append(wall)
            if not args.terse:
                print(f"  Run {len(times)}: {t:.3f} ms")

    def one():
        t, e, wall = _run()
        _keep(t, e, wall)
        return t

    def _settled(seen):
        """True once the last WARMUP_SETTLE_RUNS runs agree closely enough that
        another one would tell us nothing new."""
        if len(seen) < WARMUP_SETTLE_RUNS:
            return False
        last = seen[-WARMUP_SETTLE_RUNS:]
        mid = median(last)
        return mid > 0 and all(abs(x - mid) / mid * 100.0 <= WARMUP_SETTLE_PCT
                               for x in last)

    def warm_up():
        """Spend untimed runs until the query stops moving, then let the timed
        runs measure steady state.

        The first run doubles as the probe.  A query whose first run is slower
        than WARMUP_MAX_QUERY_MS keeps that run as its first timed sample and
        warms up no further, so the expensive queries -- the ones that already
        stop at the RUNS_MIN floor precisely because a run costs seconds -- pay
        nothing at all for this.  Only cheap queries go on to spend runs, and
        both caps below are on their measured cost, not on a per-query setting.

        Returns the number of untimed runs spent (0 when the probe was kept),
        which the baseline phase records and the testbranch phase replays.
        """
        if args.no_warmup:
            return 0
        if force_warmup is not None:
            # Replay the baseline's count, whatever this side's first run says.
            seen = []
            for _ in range(force_warmup):
                t, _e, _wall = _run()
                if t is None:
                    break
                seen.append(t)
            if seen and not args.terse:
                print(f"  ({len(seen)} warm-up runs, untimed, matching the "
                      f"baseline: {seen[0]:.3f} -> {seen[-1]:.3f} ms)")
            return len(seen)
        t, e, wall = _run()
        if t is None:
            return 0                                 # query failed; let one() report it
        if t > WARMUP_MAX_QUERY_MS:
            _keep(t, e, wall)                        # too costly to spend runs warming
            return 0
        seen, spent = [t], wall
        while (len(seen) < WARMUP_MAX and spent < args.warmup_budget
               and not _settled(seen)):
            t, _e, wall = _run()
            if t is None:
                break
            seen.append(t)
            spent += wall
        if not args.terse:
            print(f"  ({len(seen)} warm-up runs, untimed: "
                  f"{seen[0]:.3f} -> {seen[-1]:.3f} ms)")
        return len(seen)

    def drop_outlying_first_run():
        """Replace a first timed run that disagrees with the rest of its phase.

        See FIRST_RUN_OUTLIER_PCT.  Runs at most once, keeps the sample count
        unchanged (so a forced or baseline-matched count still holds), and is
        deliberately sign-agnostic: a first run that is too fast misleads exactly
        as much as one that is too slow, and in the queries too expensive to warm
        up it is usually the fast one.
        """
        if args.no_warmup or len(times) < 3:
            return
        rest = median(times[1:])
        if rest <= 0:
            return
        off = abs(times[0] - rest) / rest * 100.0
        if off <= FIRST_RUN_OUTLIER_PCT:
            return
        first = times[0]
        del times[0], explains[0], walls[0]
        t, e, wall = _run()
        if t is not None:
            times.append(t)
            explains.append(e)
            walls.append(wall)
        if not args.terse:
            got = f"{t:.3f} ms" if t is not None else "no result"
            print(f"  (run 1 was {first:.3f} ms, {off:.1f}% from the rest of the "
                  f"phase; discarded, re-run gave {got})")

    warmup_runs = warm_up()

    # Every target below counts TIMED samples, not runs performed: warm_up() may
    # already have kept one (the probe of a query too slow to warm up), and an
    # exact --runs/force_count must still mean exactly that many timed runs.
    if force_count is not None:
        while len(times) < force_count:             # match the baseline's count
            one()
        drop_outlying_first_run()
        return times, explains, warmup_runs

    if args.runs is not None:
        while len(times) < args.runs:               # exact forced count
            one()
        drop_outlying_first_run()
        return times, explains, warmup_runs

    while len(times) < RUNS_MIN:                    # dynamic floor (always >= 3)
        one()
    # Extend only while BOTH budgets are still unmet (capped at RUNS_MAX), so
    # whichever binds first stops the loop: the wall budget (eviction + prewarm +
    # query) reins in eviction-heavy queries, and the execution-time budget (pure
    # query time, eviction excluded) reins in queries that are merely cheap.
    while len(times) < RUNS_MAX and walls and (sum(walls) < args.runs_budget
                                               and sum(times) < args.runs_exec_budget):
        one()
    drop_outlying_first_run()
    return times, explains, warmup_runs


def baseline_config(args, baseline_name):
    """Everything that decides how this run measures its baseline, as stored
    in the result file.  --old-master-results reuses baseline numbers only
    from a file whose baseline_config is identical: another build, data
    directory, timing method or I/O setting makes them incomparable with
    this run's testbranch numbers."""
    bin_dir, data_dir, _source_dir, _conn = BASELINE_CONFIGS[baseline_name]
    return {
        "baseline": baseline_name,
        "bin_dir": bin_dir,
        "data_dir": data_dir,
        "tmpfs_hugepages": not args.no_tmpfs_hugepages,
        "instrument": args.instrument,
        "instrument_timing": args.instrument_timing,
        "serialize": args.serialize,
        "cursor": args.cursor,
        "bitmap": args.bitmap,
        "warmup": not args.no_warmup,
        "pin": args.pin,
        "direct_io": server_direct_io(args, on_patch=False),
        "io_method": args.io_method,
        "effective_io_concurrency": args.effective_io_concurrency,
        "io_combine_limit": args.io_combine_limit,
        "io_max_workers": args.io_max_workers,
        "io_min_workers": args.io_min_workers,
        "io_worker_launch_interval": args.io_worker_launch_interval,
        "io_max_concurrency": IO_MAX_CONCURRENCY,
        "os_cache_eviction": ("global_drop_caches" if GLOBAL_DROP_CACHES
                              else "fadvise_dontneed"),
    }


def load_most_recent_master_results(mode_name, needed_queries, config):
    """Load baseline results from the most recent benchmark JSON file for a
    given mode that measured the baseline the same way as this run.

    Only files whose "baseline_config" equals *config* count.  Among those,
    the newest file with baseline results for every one of *needed_queries*
    wins; failing that, the newest file with results for at least one.

    Args:
        mode_name: The benchmark mode to match (e.g. "benchmark_cached",
                   "worker_regress_uncached").  Files are matched by their
                   "{mode_name}_" prefix.
        needed_queries: List of query IDs that the caller wants baseline
                        results for.
        config: This run's baseline_config().

    Returns:
        tuple: (old_results dict, json_file path) or (None, None) if not found
    """
    if not os.path.exists(OUTPUT_DIR):
        return None, None

    # Find JSON files matching this mode's naming convention
    prefix = f"{mode_name}_"
    json_files = []
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith(prefix) and f.endswith(".json"):
            json_files.append(os.path.join(OUTPUT_DIR, f))

    # Sort by modification time (most recent first)
    json_files.sort(key=os.path.getmtime, reverse=True)

    chosen = partial = None
    skipped = partial_skipped = 0       # measured differently, newer than the pick
    for json_file in json_files[:500]:
        try:
            with open(json_file, "r") as f:
                old_results = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load {json_file}: {e}")
            continue

        if old_results.get("baseline_config") != config:
            skipped += 1
            continue
        old_queries = old_results.get("queries", {})
        have = sum(1 for qid in needed_queries
                   if old_queries.get(qid, {}).get("master", {}).get("min") is not None)
        if have == len(needed_queries):
            chosen = (old_results, json_file)
            break
        if have and partial is None:
            partial, partial_skipped = (old_results, json_file), skipped

    if chosen is None and partial is not None:
        chosen, skipped = partial, partial_skipped
    if skipped:
        print(f"Note: not reusing {skipped} {mode_name} result file(s) whose "
              f"baseline was measured differently (another build, data "
              f"directory, timing method, plan forcing or I/O setting)")
    return chosen or (None, None)


def print_ratio_rankings(all_ratios, args, baseline_name, baseline_label, scope=""):
    """Print NEUTRAL / IMPROVEMENTS / REGRESSIONS rankings and the OVERALL
    SUMMARY (totals + geometric mean of ratios) for a list of ratio entries.

    Each entry is a dict with query_id, name, config, ratio, master_ms and
    patch_ms (as built in run_generic_benchmark).  Factored out so the same
    rankings print per-suite and, when --queries spans suites, once more over
    the pooled set of every query run.

    scope: optional label prefixed onto section headers (e.g. "ALL SUITES — ")
           to distinguish a combined cross-suite summary from a per-suite one.
    """
    stat_label = get_stat_label()
    BOLD = "\033[1m"
    RESET = "\033[0m"

    # Classify each entry (improvement / regression / neutral).  "neutral" folds
    # in noise-level absolute differences regardless of ratio (see classify_ratio),
    # so e.g. 0.133ms vs 0.134ms is neutral, not a regression.
    for e in all_ratios:
        e["category"] = classify_ratio(e["master_ms"], e["patch_ms"], args.noise_abs_ms)
    neutral = sorted([e for e in all_ratios if e["category"] == "neutral"], key=lambda x: x["ratio"])
    improvements = sorted([e for e in all_ratios if e["category"] == "improvement"], key=lambda x: x["ratio"])
    regressions = sorted([e for e in all_ratios if e["category"] == "regression"], key=lambda x: x["ratio"], reverse=True)

    def _print_section(header, entries):
        print(f"\n{'=' * 60}")
        print(header)
        print(f"{'=' * 60}")
        if entries:
            for rank, entry in enumerate(entries, 1):
                print(f"  #{rank}  {entry['query_id']} ({entry['config']}): {entry['name']}")
                print(f"       {BOLD}{entry['ratio']:.3f}x{RESET} - {baseline_name} ({stat_label}): "
                      f"{entry['master_ms']:.3f} ms [{entry.get('master_runs', '?')} runs], "
                      f"patch ({stat_label}): {entry['patch_ms']:.3f} ms [{entry.get('patch_runs', '?')} runs]")
        else:
            print("  (none)")

    _print_section(f"{scope}NEUTRAL vs {baseline_label} (using {stat_label}) [{len(neutral)} total]", neutral)
    _print_section(f"{scope}IMPROVEMENTS vs {baseline_label} (using {stat_label}) [{len(improvements)} total]", improvements)
    _print_section(f"{scope}REGRESSIONS vs {baseline_label} (using {stat_label}) [{len(regressions)} total]", regressions)

    # Print overall summary comparing patch vs master
    # Determine which config to summarize based on benchmark settings
    if args.prefetch_disabled:
        summary_config = "prefetch=off"
        summary_label = f"patch + no prefetch vs {baseline_name}"
    else:
        summary_config = "prefetch=on"
        summary_label = f"patch + prefetch vs {baseline_name}"

    # Filter ratios for the applicable config
    config_ratios = [e for e in all_ratios if e["config"] == summary_config]

    if config_ratios:
        # Two views of the same data, printed back to back: one INCLUDING neutral
        # (noise-level) queries in the totals / ratio-of-totals / geomean /
        # best-worst, and one EXCLUDING them.  Neutral results have often-extreme
        # ratios that distort the geomean, so the net-of-neutral view is the
        # headline -- it is printed LAST for prominence.  Within each block the
        # "Number of queries", totals, ratio and geomean all describe the same
        # population (the block's `entries`), so the block is self-consistent on
        # its own.  improved/regressed are view-independent (a neutral query is
        # neither), so they sum to len(entries) in each view; only the neutral
        # count and the query total differ between the two.
        scored = [e for e in config_ratios if e["category"] != "neutral"]
        n_improved = sum(1 for e in config_ratios if e["category"] == "improvement")
        n_regressed = sum(1 for e in config_ratios if e["category"] == "regression")
        n_neutral = sum(1 for e in config_ratios if e["category"] == "neutral")

        def _print_overall(neutral_note, entries, excluded=0):
            label = f"{summary_label} ({neutral_note})" if neutral_note else summary_label
            header = f"{scope}OVERALL SUMMARY: {label}"
            bar = "=" * len(header)
            print(f"\n{bar}")
            print(header)
            print(bar)
            # Count the population the totals/ratio/geomean below are computed over.
            count_note = (f"   ({len(config_ratios)} total, {excluded} neutral excluded)"
                          if excluded else "")
            print(f"  Number of queries:             {len(entries)}{count_note}")
            if entries:
                total_master_ms = sum(e["master_ms"] for e in entries)
                total_patch_ms = sum(e["patch_ms"] for e in entries)
                total_ratio = total_patch_ms / total_master_ms
                geomean_ratio = math.exp(sum(math.log(e["ratio"]) for e in entries) / len(entries))
                best_ratio = min(e["ratio"] for e in entries)
                worst_ratio = max(e["ratio"] for e in entries)
                master_label = f"Total exec time ({baseline_name}):"
                print(f"  {master_label:<26}{total_master_ms:10.3f} ms")
                print(f"  {'Total exec time (patch):':<26}{total_patch_ms:10.3f} ms")
                print(f"  Ratio of exec time totals:     {BOLD}{total_ratio:.3f}x{RESET}")
                print(f"  Geometric mean of ratios:      {BOLD}{geomean_ratio:.3f}x{RESET}")
            print(f"  Queries improved:              {n_improved}")
            print(f"  Queries regressed:             {n_regressed}")
            if not excluded:
                print(f"  Queries neutral:               {n_neutral}")
            if entries:
                print(f"  Best case ratio:               {best_ratio:.3f}x")
                print(f"  Worst case ratio:              {worst_ratio:.3f}x")

        # With neutral queries present, print both views (included, then the
        # net-of-neutral headline last).  With none, a single unqualified block --
        # the "excluded" view would just duplicate it.
        if n_neutral:
            _print_overall("neutral included", config_ratios)
            _print_overall("neutral excluded", scored, excluded=n_neutral)
        else:
            _print_overall("", config_ratios)


def _arm_tmpfs_cleanup(tmpfs_mount):
    """Ensure the tmpfs hugepage mount is torn down even if the run aborts
    (e.g. sys.exit on a failed data check) -- it would otherwise leak.

    Registers an atexit hook and returns a callable for the normal end-of-run
    cleanup; the mount is unmounted at most once, whichever fires first.
    """
    state = {"done": False}

    def _cleanup():
        if tmpfs_mount and not state["done"]:
            state["done"] = True
            try:
                cleanup_tmpfs(tmpfs_mount)
            except Exception as e:
                print(f"Warning: tmpfs cleanup failed: {e}")

    if tmpfs_mount:
        atexit.register(_cleanup)
    return _cleanup


def run_generic_benchmark(args, queries_dict, mode_name, title,
                          verify_func=None, load_func=None,
                          sync_stats=False, tables=None,
                          expect_all_visible=True, query_ids=None):
    """Run a benchmark suite.

    This is the unified benchmark runner used by all benchmark modes
    (see BENCHMARK_SUITES registry). It handles tmpfs setup,
    data verification/loading, master runs, patch runs, results summary,
    file saving, and rankings.

    Args:
        args: Parsed command-line arguments.
        queries_dict: OrderedDict of query definitions (a suite's "queries" mapping).
        mode_name: Short mode identifier for result files (e.g. "benchmark", "worker_regress").
        title: Display title for the benchmark (e.g. "Prefetch Benchmark").
        verify_func: Function(conn_details) -> bool to verify data exists.
        load_func: Function(conn_details) to load data.
        sync_stats: If True, extract optimizer stats from master and restore to patch
                    (only needed for the default benchmark where tables are shared).
        tables: List of table names belonging to this suite (used to verify
                all-visible status after loading).
        expect_all_visible: If True (default), verify all pages are all-visible
                            after loading and VACUUM FREEZE if not.
        query_ids: Explicit list of query ids to run from queries_dict. When
                   provided (e.g. by the cross-suite --queries resolver in
                   main()), it overrides args.queries parsing; the ids are
                   assumed already validated against this suite's dict.
    """
    # --bitmap rewrites the suite's query definitions up front, so everything
    # below -- prepare, timed runs, the confirmation pass, saved explains --
    # sees the forced GUCs.
    if args.bitmap:
        queries_dict = bitmap_queries_dict(queries_dict)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Resolve baseline and testbranch configuration
    baseline_name = getattr(args, "baseline", "master")
    baseline_label = baseline_name.upper()
    baseline_bin_orig, baseline_data_dir, baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    testbranch_name = getattr(args, "testbranch", "patch")
    test_bin_orig, test_data_dir, test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]

    # master shares the patch build's data directory (see MASTER_DATA_DIR).
    # One set of files means one catalog: there are no optimizer statistics to
    # copy from the baseline to the testbranch, and the data is verified once.
    shared_data_dir = (os.path.realpath(baseline_data_dir)
                       == os.path.realpath(test_data_dir))
    if shared_data_dir:
        sync_stats = False

    # Sampled up front so the value describes the run itself.
    nvme_awake = nvme_kept_awake()
    note_nvme_power_state(args, nvme_awake)

    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    master_bin = baseline_bin_orig
    patch_bin = test_bin_orig
    cleanup_tmpfs_now = lambda: None   # replaced once tmpfs exists; armed vs aborts

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        cleanup_tmpfs_now = _arm_tmpfs_cleanup(tmpfs_mount)
        master_bin = copy_binaries_to_tmpfs(baseline_bin_orig, tmpfs_mount, "baseline")
        patch_bin = copy_binaries_to_tmpfs(test_bin_orig, tmpfs_mount, "testbranch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  baseline ({baseline_name}): {master_bin}")
        print(f"  testbranch ({testbranch_name}): {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    # Parse query selection
    if query_ids is not None:
        # Explicit per-suite selection resolved by the caller (cross-suite
        # --queries); already validated against this suite's dict.
        selected_queries = list(query_ids)
    elif args.queries:
        selected_queries = [q.strip().upper() for q in args.queries.split(",")]
        for q in selected_queries:
            if q not in queries_dict:
                print(f"Error: Unknown query '{q}'. Available: {', '.join(queries_dict.keys())}")
                sys.exit(1)
    else:
        selected_queries = list(queries_dict.keys())

    # Handle --old-master-results: load previous results and filter queries.
    # use_old_master is this suite's decision alone: args is shared by every
    # suite of a --queries run, and a suite with no saved results must not
    # make the next one re-run its baseline.
    old_master_data = None
    old_master_file = None
    old_results = None
    base_cfg = baseline_config(args, baseline_name)
    use_old_master = args.old_master_results
    if use_old_master:
        old_results, old_master_file = load_most_recent_master_results(
            mode_name, selected_queries, base_cfg)
        if old_results is None:
            print("No previous master results found, will run master fresh")
            use_old_master = False
            # preflight_servers() skipped the baseline, expecting reuse.
            preflight_data_dir(baseline_bin_orig, baseline_name, baseline_data_dir)
        else:
            # Extract master data from old results.  The file has results
            # for at least one of the selected queries.
            old_master_data = {}
            old_queries = old_results.get("queries", {})
            for qid in list(selected_queries):
                if qid in old_queries and old_queries[qid].get("master", {}).get("min") is not None:
                    old_master_data[qid] = old_queries[qid]["master"]
                else:
                    print(f"Warning: Query {qid} has no master results in previous run, skipping")
                    selected_queries.remove(qid)

            print(f"\nUsing master results from: {old_master_file}")
            old_hash = old_results.get("master_hash", "unknown")
            now_hash = get_git_hash(baseline_source_dir)
            print(f"  Original master hash: {old_hash}"
                  + (f" ({baseline_name} source tree now at {now_hash})"
                     if now_hash != old_hash else ""))
            print(f"  Queries with results: {', '.join(selected_queries)}")

    print(f"\n{'=' * 60}")
    print(title)
    print(f"{'=' * 60}")
    mode_word = ("\033[32m💵  cached\033[0m" if args.cached
                 else "\033[36m🧊  uncached, cold indexes (--no-cached-index)\033[0m"
                 if args.no_cached_index
                 else "\033[34m💾  uncached\033[0m")
    print(f"Mode: {mode_word}")
    if args.bitmap:
        print("Plan forcing: bitmap scans (--bitmap; index_only queries "
              "keep their index-only scans)")
    print(f"Queries: {', '.join(selected_queries)}")
    if args.runs is not None:
        print(f"Runs per query: {args.runs} (forced)")
    else:
        print(f"Runs per query: dynamic (min {RUNS_MIN}, up to {RUNS_MAX}, "
              f"wall budget {args.runs_budget:.0f} ms or exec budget "
              f"{args.runs_exec_budget:.0f} ms)")
    print(f"Backend pinning: {pinning_summary(args)}")
    print(f"Timing method: {timing_method_label(args)}")
    print(f"{'=' * 60}\n")

    # Get git hashes
    master_hash = get_git_hash(baseline_source_dir)
    patch_hash = get_git_hash(test_source_dir)
    if not args.terse:
        print(f"Baseline ({baseline_name}) git hash: {master_hash}")
        print(f"Testbranch ({testbranch_name}) git hash: {patch_hash}")
        if shared_data_dir:
            print(f"Data directory: {test_data_dir} (shared by both builds)")
        print_io_settings(args)

    # Verify/load data on each server (one at a time due to memory constraints)
    master_stats = None
    master_version = None
    patch_version = None
    if not args.skip_load and verify_func and load_func:
        # verify_func takes only conn_details for every suite.  sync_stats (the
        # default benchmark) additionally copies optimizer statistics from the
        # baseline to the testbranch so plans match; it no longer affects how data
        # is verified or loaded.

        # Whether the baseline server has to be started at all: to verify (and
        # if need be load) the data, and under sync_stats to extract the
        # statistics.  --old-master-results never measures the baseline, so it
        # is started only when nothing else can do the job: saved statistics
        # cover the sync_stats case, and with a shared data directory the
        # testbranch verifies the very same files.
        verify_on_baseline = True
        if use_old_master and old_results:
            if sync_stats:
                # Load saved statistics from old results instead of starting master
                master_stats = old_results.get("master_stats")
                if master_stats:
                    print(f"\nUsing saved master statistics from previous results")
                    verify_on_baseline = False
                else:
                    print("Warning: Old results have no saved master_stats, starting master to extract them")
            elif shared_data_dir:
                verify_on_baseline = False

        if verify_on_baseline:
            print(f"\n--- Verifying data on baseline ({baseline_name}) ---")
            start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
            if not verify_func(baseline_conn):
                print(f"Loading data on {baseline_name}...")
                load_func(baseline_conn)
                if expect_all_visible and tables:
                    ensure_all_visible_after_load(baseline_conn, tables)
            if sync_stats:
                master_stats = extract_statistics(baseline_conn)
            master_version = get_pg_version(baseline_conn)
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)
        else:
            master_version = old_results.get("master_version")

        if shared_data_dir and verify_on_baseline:
            print(f"\n--- Testbranch ({testbranch_name}) shares the baseline's "
                  f"data directory: verified above ---")
        else:
            print(f"\n--- Verifying data on testbranch ({testbranch_name}) ---")
            start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
            if not verify_func(test_conn):
                print(f"Loading data on {testbranch_name}...")
                load_func(test_conn)
                if expect_all_visible and tables:
                    ensure_all_visible_after_load(test_conn, tables)
            if sync_stats:
                restore_statistics(test_conn, master_stats)
            patch_version = get_pg_version(test_conn)
            stop_server(patch_bin, test_data_dir)
            time.sleep(2)

    # Results storage
    # Use old master hash if using old master results
    effective_master_hash = master_hash
    if use_old_master and old_results:
        effective_master_hash = old_results.get("master_hash", master_hash)
    results = {
        "timestamp": datetime.now().isoformat(),
        "baseline": baseline_name,
        "testbranch": testbranch_name,
        "master_hash": effective_master_hash,
        "patch_hash": patch_hash,
        "master_version": master_version,
        "patch_version": patch_version,
        "mode": mode_name,
        "bitmap": args.bitmap,
        "runs": args.runs if args.runs is not None else "dynamic",
        "runs_budget": args.runs_budget,
        "runs_exec_budget": args.runs_exec_budget,
        "noise_abs_ms": args.noise_abs_ms,
        "pin": args.pin,
        "timing": timing_method_label(args),
        "io_settings": {
            "effective_io_concurrency": args.effective_io_concurrency,
            "io_combine_limit": args.io_combine_limit,
            "io_max_combine_limit": 128,
            "io_max_concurrency": IO_MAX_CONCURRENCY,
            "io_max_workers": args.io_max_workers,
            "io_min_workers": args.io_min_workers,
            "io_worker_launch_interval": args.io_worker_launch_interval,
            "io_method": args.io_method,
            "direct_io": getattr(args, "direct_io", False),
            "patch_direct": getattr(args, "patch_direct", False),
        },
        "queries": {},
        "master_results_from": old_master_file if use_old_master else None,
        "baseline_config": base_cfg,
        "master_stats": master_stats,
        "shared_data_dir": shared_data_dir,
        "nvme_awake": nvme_awake,
        # Uncached runs became cold two different ways over the life of this
        # script.  Record which, so results taken before and after the change
        # are never silently compared: dropping the whole page cache also made
        # every timed query re-fault the postgres binary and the catalogs, which
        # the targeted eviction leaves resident.
        "os_cache_eviction": ("global_drop_caches" if GLOBAL_DROP_CACHES
                              else "fadvise_dontneed"),
    }

    # Initialize query results structure
    for query_id in selected_queries:
        query_def = queries_dict[query_id]
        # Use old master data if available, otherwise initialize empty
        if old_master_data and query_id in old_master_data:
            master_result = old_master_data[query_id].copy()
        else:
            master_result = {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None,
                             "warmup_runs": None}
        results["queries"][query_id] = {
            "name": query_def["name"],
            "master": master_result,
            "patch_off": {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None,
                          "warmup_runs": None},
            "patch_on": {"times": [], "explains": [], "avg": None, "min": None, "max": None, "explain": None,
                         "warmup_runs": None},
        }

    # Run all queries on master (skip if using old master results)
    if use_old_master:
        print(f"\n{'=' * 60}")
        print(f"SKIPPING {baseline_label} (using previous results)")
        print(f"{'=' * 60}")
        master_start_time = 0
        master_end_time = 0
        # Get master version from old results if available
        if old_results and old_results.get("master_version"):
            master_version = old_results["master_version"]
    else:
        print(f"\n{'=' * 60}")
        print(f"Running all queries on BASELINE ({baseline_name})")
        print(f"{'=' * 60}")
        master_start_time = time.time()
        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
        try:
            master_conn = bench_connect(baseline_conn)
            pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            if master_version is None:
                master_version = get_pg_version(baseline_conn)
            with master_conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

            master_io = explain_supports_io(baseline_conn) if args.instrument else False
            if args.instrument and not master_io:
                print(f"Note: {baseline_name} EXPLAIN has no IO option; "
                      f"running it without IO instrumentation")

            for query_id in selected_queries:
                query_def = queries_dict[query_id]
                print(f"\n{query_id}: {query_def['name']}...")
                prepare_query(master_conn, query_def["sql"],
                              gucs=query_def.get("gucs", {}), is_master=True,
                              stmt_name=bench_stmt_name(query_id))
                times, explains, warmup_runs = measure_query(
                    master_conn, query_def, args, is_master=True,
                    prefetch_setting=None, io_supported=master_io,
                    on_patch=False)
                results["queries"][query_id]["master"]["times"].extend(times)
                results["queries"][query_id]["master"]["explains"].extend(explains)
                results["queries"][query_id]["master"]["warmup_runs"] = warmup_runs
                deallocate_query(master_conn)

            master_conn.close()
        finally:
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)
        master_end_time = time.time()

    # Run all queries on testbranch (both prefetch=off and prefetch=on, or plain if no prefetch GUC)
    print(f"\n{'=' * 60}")
    print(f"Running all queries on TESTBRANCH ({testbranch_name})")
    print(f"{'=' * 60}")
    patch_start_time = time.time()
    start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
    try:
        patch_conn = bench_connect(test_conn)
        pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
        if patch_version is None:
            patch_version = get_pg_version(test_conn)
        with patch_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

        # Tolerate a testbranch build that lacks the prefetch GUC (warns + treats
        # it as a baseline).  Persists to the regression re-confirm phase below.
        test_has_prefetch = verify_test_has_prefetch(
            patch_conn, test_has_prefetch, testbranch_name, args.prefetch_disabled)

        patch_io = explain_supports_io(test_conn) if args.instrument else False
        if args.instrument and not patch_io:
            print(f"Note: {testbranch_name} EXPLAIN has no IO option; "
                  f"running it without IO instrumentation")

        for query_id in selected_queries:
            query_def = queries_dict[query_id]
            # When testbranch lacks the prefetch GUC, treat it like a baseline
            test_is_master = not test_has_prefetch
            # Match the baseline's run count so the slower side never gets fewer
            # dynamic runs (which would bias its min upward).  None -> dynamic.
            master_n = len(results["queries"][query_id]["master"]["times"])
            patch_count = master_n if master_n > 0 else None
            # Likewise replay the baseline's warm-up run count, so both sides'
            # timed samples start at the same execution number.  None (no
            # baseline numbers from this run) leaves warm_up() to decide.
            patch_warmup = (results["queries"][query_id]["master"].get("warmup_runs")
                            if master_n > 0 else None)
            prepare_query(patch_conn, query_def["sql"],
                          gucs=query_def.get("gucs", {}), is_master=test_is_master,
                          stmt_name=bench_stmt_name(query_id))

            # Run with prefetch OFF (skip if --prefetch-only or testbranch has no prefetch GUC)
            if not args.prefetch_only and test_has_prefetch:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off)...")
                times, explains, warmup_runs = measure_query(
                    patch_conn, query_def, args, is_master=False,
                    prefetch_setting="off", io_supported=patch_io,
                    on_patch=True, force_count=patch_count,
                    force_warmup=patch_warmup)
                results["queries"][query_id]["patch_off"]["times"].extend(times)
                results["queries"][query_id]["patch_off"]["explains"].extend(explains)
                results["queries"][query_id]["patch_off"]["warmup_runs"] = warmup_runs

            # Run with prefetch ON (skip if --prefetch-disabled)
            # When testbranch has no prefetch GUC, run queries plain (results stored in patch_on)
            if not args.prefetch_disabled:
                if test_has_prefetch:
                    print(f"\n{query_id}: {query_def['name']} (prefetch=on)...")
                    pf_setting = "on"
                else:
                    print(f"\n{query_id}: {query_def['name']}...")
                    pf_setting = None
                times, explains, warmup_runs = measure_query(
                    patch_conn, query_def, args, is_master=test_is_master,
                    prefetch_setting=pf_setting, io_supported=patch_io,
                    on_patch=True, force_count=patch_count,
                    force_warmup=patch_warmup)
                results["queries"][query_id]["patch_on"]["times"].extend(times)
                results["queries"][query_id]["patch_on"]["explains"].extend(explains)
                results["queries"][query_id]["patch_on"]["warmup_runs"] = warmup_runs

            deallocate_query(patch_conn)

        patch_conn.close()
    finally:
        stop_server(patch_bin, test_data_dir)
        time.sleep(2)
    patch_end_time = time.time()

    # --- Auto-confirmation pass ---------------------------------------------
    # The baseline and patch phases run minutes apart, so a query can be flagged
    # as a regression just for catching a sustained transient that min() (which
    # only filters brief blips) cannot remove.  Re-run only the apparent
    # REGRESSIONS (patch slower than master) on both servers and POOL the new
    # samples into the existing times: min() over the decorrelated re-measurement
    # washes out a one-off transient on either side, while a genuine regression
    # survives.  Improvements (patch faster) are the expected direction for a
    # prefetch win, so they are not re-confirmed -- only the surprising direction
    # is worth the cost.  This deliberately just adds samples (gentle) rather
    # than discarding a result the moment one retry clears, the way
    # --stress-test does.  Skipped with --no-confirm, with an exact --runs count,
    # with --old-master-results (no live baseline to re-measure), and with an
    # explicit --queries list (a targeted check whose queries were usually
    # picked BECAUSE they came out as regressions; the caller has already
    # decided to look at them, and the pass would pad every quick check with
    # a second round on both servers).  --confirm restores it for --queries.
    do_confirm = (not args.no_confirm and not use_old_master
                  and args.runs is None
                  and (not args.queries or args.confirm))
    flagged = []
    if do_confirm:
        for query_id in selected_queries:
            qr = results["queries"][query_id]
            m = get_representative(qr["master"]["times"])
            if not m:
                continue
            for cfg in ("patch_off", "patch_on"):
                p = get_representative(qr[cfg]["times"])
                if p and classify_ratio(m, p, args.noise_abs_ms) == "regression":
                    flagged.append(query_id)
                    break

    confirm_duration = 0.0
    if flagged:
        confirm_start_time = time.time()
        print(f"\n{'=' * 60}")
        print("AUTO-CONFIRMATION PASS")
        print(f"{'=' * 60}")
        print(f"Re-measuring {len(flagged)} apparent "
              f"{'regression' if len(flagged) == 1 else 'regressions'} "
              f"(patch slower than {baseline_name}) to filter "
              f"transients: {', '.join(flagged)}")

        # Snapshot pre-confirmation representatives for the before/after report.
        before = {}
        for query_id in flagged:
            qr = results["queries"][query_id]
            before[query_id] = {
                cfg: get_representative(qr[cfg]["times"])
                for cfg in ("master", "patch_off", "patch_on")
            }

        # Re-measure on the baseline; record each query's new sample count and
        # warm-up run count so the testbranch re-measure can match them (keeps
        # the pooled counts symmetric).
        confirm_n = {}
        confirm_warmup = {}
        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
        try:
            mconn = bench_connect(baseline_conn)
            pin_backend(mconn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            with mconn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
            m_io = explain_supports_io(baseline_conn) if args.instrument else False
            for query_id in flagged:
                qd = queries_dict[query_id]
                print(f"\n{query_id}: {qd['name']} (confirm {baseline_name})...")
                prepare_query(mconn, qd["sql"], gucs=qd.get("gucs", {}),
                              is_master=True, stmt_name=bench_stmt_name(query_id))
                times, explains, warmup_runs = measure_query(
                    mconn, qd, args, is_master=True, prefetch_setting=None,
                    io_supported=m_io, on_patch=False)
                confirm_n[query_id] = len(times)
                confirm_warmup[query_id] = warmup_runs
                results["queries"][query_id]["master"]["times"].extend(times)
                results["queries"][query_id]["master"]["explains"].extend(explains)
                deallocate_query(mconn)
            mconn.close()
        finally:
            stop_server(master_bin, baseline_data_dir)
            time.sleep(2)

        # Re-measure on the testbranch, matching the baseline's confirm count.
        start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
        try:
            pconn = bench_connect(test_conn)
            pin_backend(pconn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
            with pconn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
            p_io = explain_supports_io(test_conn) if args.instrument else False
            test_is_master = not test_has_prefetch
            for query_id in flagged:
                qd = queries_dict[query_id]
                fc = confirm_n.get(query_id) or None
                fw = confirm_warmup.get(query_id) if fc else None
                prepare_query(pconn, qd["sql"], gucs=qd.get("gucs", {}),
                              is_master=test_is_master, stmt_name=bench_stmt_name(query_id))
                if not args.prefetch_only and test_has_prefetch:
                    print(f"\n{query_id}: {qd['name']} (confirm prefetch=off)...")
                    times, explains, _warmup_runs = measure_query(
                        pconn, qd, args, is_master=False, prefetch_setting="off",
                        io_supported=p_io, on_patch=True, force_count=fc,
                        force_warmup=fw)
                    results["queries"][query_id]["patch_off"]["times"].extend(times)
                    results["queries"][query_id]["patch_off"]["explains"].extend(explains)
                if not args.prefetch_disabled:
                    pf = "on" if test_has_prefetch else None
                    print(f"\n{query_id}: {qd['name']} (confirm prefetch=on)...")
                    times, explains, _warmup_runs = measure_query(
                        pconn, qd, args, is_master=test_is_master, prefetch_setting=pf,
                        io_supported=p_io, on_patch=True, force_count=fc,
                        force_warmup=fw)
                    results["queries"][query_id]["patch_on"]["times"].extend(times)
                    results["queries"][query_id]["patch_on"]["explains"].extend(explains)
                deallocate_query(pconn)
            pconn.close()
        finally:
            stop_server(patch_bin, test_data_dir)
            time.sleep(2)
        confirm_duration = time.time() - confirm_start_time

        # Report before -> after (pooled min) and whether the flag held.
        print(f"\n{'=' * 60}")
        print("CONFIRMATION RESULTS (pooled min, before -> after)")
        print(f"{'=' * 60}")
        for query_id in flagged:
            qr = results["queries"][query_id]
            m_before = before[query_id]["master"]
            m_after = get_representative(qr["master"]["times"])
            for cfg, label in (("patch_off", "prefetch=off"), ("patch_on", "prefetch=on")):
                p_before = before[query_id][cfg]
                if not p_before:
                    continue
                p_after = get_representative(qr[cfg]["times"])
                cat_before = classify_ratio(m_before, p_before, args.noise_abs_ms)
                cat_after = classify_ratio(m_after, p_after, args.noise_abs_ms)
                rb = p_before / m_before if m_before else float("nan")
                ra = p_after / m_after if m_after else float("nan")
                verdict = (f"{cat_before} confirmed" if cat_after == cat_before
                           else f"{cat_before} -> {cat_after}")
                print(f"  {query_id} ({label}): {rb:.3f}x -> {ra:.3f}x  [{verdict}]")

    # Cleanup tmpfs if it was created (also armed via atexit against aborts)
    cleanup_tmpfs_now()

    # Calculate statistics
    for query_id in selected_queries:
        query_results = results["queries"][query_id]
        for config in ["master", "patch_off", "patch_on"]:
            times = query_results[config]["times"]
            if times:
                query_results[config]["avg"] = mean(times)
                query_results[config]["min"] = min(times)
                query_results[config]["median"] = median(times)
                query_results[config]["max"] = max(times)
            # Select the explain output from the run closest to the representative value
            explains = query_results[config].get("explains", [])
            if times and explains:
                rep = get_representative(times)
                best_idx = min(range(len(times)), key=lambda i: abs(times[i] - rep))
                query_results[config]["explain"] = explains[best_idx]
            # Drop the explains list before saving to JSON (it's bulky and redundant)
            query_results[config].pop("explains", None)

    # Print per-query detail (suppressed in terse mode)
    if not args.terse:
        print(f"\n{'=' * 60}")
        print("RESULTS SUMMARY")
        print(f"{'=' * 60}")

        for query_id in selected_queries:
            query_results = results["queries"][query_id]

            stat_label = get_stat_label()
            master_rep = get_representative(query_results["master"]["times"])
            patch_off_rep = get_representative(query_results["patch_off"]["times"])
            patch_on_rep = get_representative(query_results["patch_on"]["times"])

            # ANSI bold escape codes
            BOLD = "\033[1m"
            RESET = "\033[0m"

            print(f"\n{BOLD}{query_id}: {query_results['name']}{RESET}")
            if master_rep:
                print(f"  {baseline_name} ({stat_label}):               {master_rep:10.3f} ms "
                      f"(avg={query_results['master']['avg']:.3f}, max={query_results['master']['max']:.3f})")
            if patch_off_rep and master_rep:
                ratio_off = patch_off_rep / master_rep
                print(f"  patch (prefetch=off) ({stat_label}): {patch_off_rep:10.3f} ms "
                      f"(avg={query_results['patch_off']['avg']:.3f}, max={query_results['patch_off']['max']:.3f}) "
                      f"[{BOLD}{ratio_off:.3f}x{RESET} vs {baseline_name}]")
            if patch_on_rep and master_rep:
                ratio_on = patch_on_rep / master_rep
                print(f"  patch (prefetch=on) ({stat_label}):  {patch_on_rep:10.3f} ms "
                      f"(avg={query_results['patch_on']['avg']:.3f}, max={query_results['patch_on']['max']:.3f}) "
                      f"[{BOLD}{ratio_on:.3f}x{RESET} vs {baseline_name}]")

            # Print query text and EXPLAIN ANALYZE outputs
            # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
            print()
            query_sql = queries_dict[query_id]["sql"].strip()
            print("  Query:")
            for line in query_sql.split('\n'):
                print(f"    {line.strip()}")
            print()
            if query_results["master"]["explain"]:
                print(f"  {baseline_name} EXPLAIN ANALYZE:")
                for line in query_results["master"]["explain"].split('\n'):
                    print(f"    {line}")
            if args.prefetch_disabled:
                if query_results["patch_off"]["explain"]:
                    print()
                    print("  patch (prefetch=off) EXPLAIN ANALYZE:")
                    for line in query_results["patch_off"]["explain"].split('\n'):
                        print(f"    {line}")
            else:
                if query_results["patch_on"]["explain"]:
                    print()
                    print("  patch (prefetch=on) EXPLAIN ANALYZE:")
                    for line in query_results["patch_on"]["explain"].split('\n'):
                        print(f"    {line}")

    # Save results
    # Update versions in case they were fetched during benchmark run (--skip-load)
    results["master_version"] = master_version
    results["patch_version"] = patch_version

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_file = os.path.join(OUTPUT_DIR, f"{mode_name}_{timestamp}.json")
    txt_file = os.path.join(OUTPUT_DIR, f"{mode_name}_{timestamp}.txt")

    # Save JSON
    with open(json_file, "w") as f:
        json.dump(results, f, indent=2)
    if not args.terse:
        print(f"\nResults saved to: {json_file}")

    # Save human-readable text
    with open(txt_file, "w") as f:
        f.write("=" * 70 + "\n")
        f.write(f"{title} Results\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Timestamp: {results['timestamp']}\n")
        f.write(f"{baseline_label} git hash: {results['master_hash']}\n")
        f.write(f"Patch git hash: {results['patch_hash']}\n")
        f.write(f"Mode: {results['mode']}\n")
        f.write(f"Runs per query: {results['runs']}\n")
        f.write(f"Backend pinning: {'on' if results.get('pin') else 'off'}\n")
        f.write(f"Timing method: {results.get('timing', 'execute-drain')}\n\n")

        stat_label = get_stat_label()
        for query_id, qr in results["queries"].items():
            f.write(f"\n{query_id}: {qr['name']}\n")
            f.write("-" * 50 + "\n")

            master_rep = get_representative(qr["master"]["times"])
            patch_off_rep = get_representative(qr["patch_off"]["times"])
            patch_on_rep = get_representative(qr["patch_on"]["times"])

            if master_rep:
                f.write(f"  {baseline_name} ({stat_label}):               {master_rep:10.3f} ms "
                        f"(avg={qr['master']['avg']:.3f}, max={qr['master']['max']:.3f})\n")
            if patch_off_rep and master_rep:
                ratio_off = patch_off_rep / master_rep
                f.write(f"  patch (prefetch=off) ({stat_label}): {patch_off_rep:10.3f} ms "
                        f"(avg={qr['patch_off']['avg']:.3f}, max={qr['patch_off']['max']:.3f}) "
                        f"[{ratio_off:.3f}x vs {baseline_name}]\n")
            if patch_on_rep and master_rep:
                ratio_on = patch_on_rep / master_rep
                f.write(f"  patch (prefetch=on) ({stat_label}):  {patch_on_rep:10.3f} ms "
                        f"(avg={qr['patch_on']['avg']:.3f}, max={qr['patch_on']['max']:.3f}) "
                        f"[{ratio_on:.3f}x vs {baseline_name}]\n")

            # Write query text and EXPLAIN ANALYZE outputs
            # Show prefetch=off only if --prefetch-disabled, otherwise show prefetch=on
            f.write("\n")
            query_sql = queries_dict[query_id]["sql"].strip()
            f.write("  Query:\n")
            for line in query_sql.split('\n'):
                f.write(f"    {line.strip()}\n")
            f.write("\n")
            if qr["master"]["explain"]:
                f.write(f"  {baseline_name} EXPLAIN ANALYZE:\n")
                for line in qr["master"]["explain"].split('\n'):
                    f.write(f"    {line}\n")
            if args.prefetch_disabled:
                if qr["patch_off"]["explain"]:
                    f.write("\n")
                    f.write("  patch (prefetch=off) EXPLAIN ANALYZE:\n")
                    for line in qr["patch_off"]["explain"].split('\n'):
                        f.write(f"    {line}\n")
            else:
                if qr["patch_on"]["explain"]:
                    f.write("\n")
                    f.write("  patch (prefetch=on) EXPLAIN ANALYZE:\n")
                    for line in qr["patch_on"]["explain"].split('\n'):
                        f.write(f"    {line}\n")

    if not args.terse:
        print(f"Results saved to: {txt_file}")

    # Update latest symlink.  lexists(), not exists(): a link whose target
    # was deleted still has to go.
    latest_link = os.path.join(OUTPUT_DIR, "latest.txt")
    if os.path.lexists(latest_link):
        os.remove(latest_link)
    os.symlink(os.path.basename(txt_file), latest_link)

    # Print total run times
    master_duration = master_end_time - master_start_time
    patch_duration = patch_end_time - patch_start_time
    total_duration = master_duration + patch_duration + confirm_duration

    if not args.terse:
        print(f"\n{'=' * 60}")
        print("BENCHMARK RUN TIMES (excluding data loading)")
        print(f"{'=' * 60}")
        durations = [(baseline_label, master_duration), ("Patch", patch_duration)]
        if confirm_duration:
            durations.append(("Confirm", confirm_duration))
        durations.append(("Total", total_duration))
        for label, seconds in durations:
            print(f"  {label + ':':11s}{seconds:10.1f} seconds ({_format_duration(seconds)})")

    # Collect all patch runs with their ratios vs master (using min or median as representative)
    # Each patch configuration (prefetch=off, prefetch=on) is treated independently
    all_ratios = []
    for query_id in selected_queries:
        qr = results["queries"][query_id]
        master_rep = get_representative(qr["master"]["times"])
        if not master_rep:
            continue

        # patch (prefetch=off)
        patch_off_rep = get_representative(qr["patch_off"]["times"])
        if patch_off_rep:
            ratio = patch_off_rep / master_rep
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=off",
                "ratio": ratio,
                "master_ms": master_rep,
                "patch_ms": patch_off_rep,
                "master_runs": len(qr["master"]["times"]),
                "patch_runs": len(qr["patch_off"]["times"]),
            })

        # patch (prefetch=on)
        patch_on_rep = get_representative(qr["patch_on"]["times"])
        if patch_on_rep:
            ratio = patch_on_rep / master_rep
            all_ratios.append({
                "query_id": query_id,
                "name": qr["name"],
                "config": "prefetch=on",
                "ratio": ratio,
                "master_ms": master_rep,
                "patch_ms": patch_on_rep,
                "master_runs": len(qr["master"]["times"]),
                "patch_runs": len(qr["patch_on"]["times"]),
            })

    print_ratio_rankings(all_ratios, args, baseline_name, baseline_label)

    # Return the per-query ratio entries so a cross-suite caller can pool them
    # into one combined ranking + geomean (see main()'s --queries path).
    return all_ratios


# --- Plan verification (--verify) ------------------------------------------
#
# Every EXPLAIN (ANALYZE, FORMAT JSON) plan node is reduced to just the fields
# that must agree between the baseline and the testbranch for a timing
# comparison to be meaningful.  Anything else (buffers, I/O counters, timing,
# per-worker detail) is dropped, so it can never cause a spurious mismatch.
PLAN_SHAPE_KEYS = (
    "Node Type", "Parent Relationship", "Join Type", "Strategy", "Partial Mode",
    "Scan Direction", "Relation Name", "Index Name", "Alias",
)
PLAN_EXEC_KEYS = (
    "Actual Rows", "Actual Loops",
    "Rows Removed by Filter", "Rows Removed by Join Filter",
    "Rows Removed by Index Recheck",
)


def plan_signature(node):
    """Reduce one JSON plan node (recursively) to the compared fields."""
    sig = {k: node[k] for k in PLAN_SHAPE_KEYS + PLAN_EXEC_KEYS if k in node}
    sig["Plans"] = [plan_signature(child) for child in node.get("Plans", [])]
    return sig


def plan_signature_lines(sig, depth=0):
    """Render a signature as an indented tree (one line per node)."""
    head = sig.get("Node Type", "?")
    for k in ("Strategy", "Partial Mode", "Join Type", "Scan Direction"):
        if k in sig and sig[k] not in ("Plain", "Simple", "Inner", "Forward"):
            head += f" {sig[k]}"
    if "Index Name" in sig:
        head += f" using {sig['Index Name']}"
    if "Relation Name" in sig:
        head += f" on {sig['Relation Name']}"
        if sig.get("Alias") and sig["Alias"] != sig["Relation Name"]:
            head += f" {sig['Alias']}"
    detail = []
    if "Actual Rows" in sig:
        detail.append(f"rows={sig['Actual Rows']} loops={sig.get('Actual Loops', '?')}")
    for k in PLAN_EXEC_KEYS[2:]:
        if k in sig:
            detail.append(f"{k}: {sig[k]}")
    line = "  " * depth + head
    if detail:
        line += "  (" + "; ".join(detail) + ")"
    lines = [line]
    for child in sig["Plans"]:
        lines.extend(plan_signature_lines(child, depth + 1))
    return lines


def compare_plan_signatures(base, test, path=""):
    """Return a list of human-readable differences between two signatures.

    Structural differences (different node type, different child count) stop
    the descent at that node: children of a differently-shaped subtree are not
    comparable, and one clear message beats a cascade.
    """
    diffs = []
    here = path or base.get("Node Type", "?")
    if base.get("Node Type") != test.get("Node Type"):
        diffs.append(f"{here}: node type {base.get('Node Type')!r} vs "
                     f"{test.get('Node Type')!r}")
        return diffs
    for k in PLAN_SHAPE_KEYS + PLAN_EXEC_KEYS:
        if k == "Node Type":
            continue
        bv, tv = base.get(k), test.get(k)
        if bv != tv:
            diffs.append(f"{here}: {k} {bv!r} vs {tv!r}")
    nb, nt = len(base["Plans"]), len(test["Plans"])
    if nb != nt:
        diffs.append(f"{here}: {nb} child node(s) vs {nt}")
        return diffs
    for i, (bc, tc) in enumerate(zip(base["Plans"], test["Plans"])):
        child_path = f"{here} > [{i}] {bc.get('Node Type', '?')}"
        diffs.extend(compare_plan_signatures(bc, tc, child_path))
    return diffs


def capture_plan(conn, query_def, is_master, prefetch_setting):
    """Prewarm the query's relations, then EXPLAIN ANALYZE the prepared
    statement once and return its plan signature (a dict) -- the same cache
    preparation as a cached benchmark run, without timing anything."""
    prewarm_relations(conn, query_def.get("prewarm_indexes", []))
    prewarm_relations(conn, query_def.get("prewarm_tables", []), include_vm=True)
    gucs = query_def.get("gucs", {})
    set_gucs(conn, gucs, is_master=is_master, prefetch_setting=prefetch_setting)
    # Reset the GUCs only on success.  After an error the transaction is
    # aborted, so a RESET would fail too, and its error would replace the one
    # the caller records; the caller's rollback undoes the SETs anyway.
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, "
                    f"FORMAT JSON) EXECUTE {_active_stmt_name}")
        (doc,) = cur.fetchone()
    if gucs:
        reset_gucs(conn, gucs)
    if isinstance(doc, str):
        doc = json.loads(doc)
    return plan_signature(doc[0]["Plan"])


def run_verify(args, queries_dict, mode_name, title, verify_func=None,
               load_func=None, query_ids=None):
    """--verify: check that baseline and testbranch agree on each query's plan.

    Runs every selected query once, cached, on the baseline and then on the
    testbranch (prefetch=on, plus prefetch=off when that config is selected),
    capturing EXPLAIN (ANALYZE, FORMAT JSON) of the very prepared statement the
    benchmark would time.  Compares plan shape, the scan nodes' relations and
    indexes, actual rows/loops and rows removed by filters at every node.

    Returns the list of mismatch records (empty when everything agrees).
    """
    # Same rewrite as run_generic_benchmark(): --verify --bitmap checks the
    # plans a --bitmap benchmark run would time.
    if args.bitmap:
        queries_dict = bitmap_queries_dict(queries_dict)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    baseline_name = getattr(args, "baseline", "master")
    baseline_bin, baseline_data_dir, baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    testbranch_name = getattr(args, "testbranch", "patch")
    patch_bin, test_data_dir, test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]
    shared_data_dir = (os.path.realpath(baseline_data_dir)
                       == os.path.realpath(test_data_dir))

    if query_ids is not None:
        selected_queries = list(query_ids)
    elif args.queries:
        selected_queries = [q.strip().upper() for q in args.queries.split(",")]
        for q in selected_queries:
            if q not in queries_dict:
                print(f"Error: Unknown query '{q}'. Available: {', '.join(queries_dict.keys())}")
                sys.exit(1)
    else:
        selected_queries = list(queries_dict.keys())

    print(f"\n{'=' * 60}")
    print(f"{title}: PLAN VERIFICATION")
    print(f"{'=' * 60}")
    print(f"Queries: {', '.join(selected_queries)}")
    print(f"One cached run per query and build; no timing.")
    if args.bitmap:
        print("Plan forcing: bitmap scans (--bitmap; index_only queries "
              "keep their index-only scans)")
    print(f"{'=' * 60}\n")
    master_hash = get_git_hash(baseline_source_dir)
    patch_hash = get_git_hash(test_source_dir)
    print(f"Baseline ({baseline_name}) git hash: {master_hash}")
    print(f"Testbranch ({testbranch_name}) git hash: {patch_hash}")
    print_io_settings(args)

    plans = {qid: {} for qid in selected_queries}   # qid -> config -> signature
    errors = {}                                     # (qid, config) -> message

    def _ensure_data(conn_details, name):
        if args.skip_load or not (verify_func and load_func):
            return
        if not verify_func(conn_details):
            print(f"Loading data on {name}...")
            load_func(conn_details)

    def _record_error(conn, query_id, configs, e):
        conn.rollback()
        msg = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
        for config in configs:
            errors[(query_id, config)] = msg
        print(f"  ERROR ({query_id}): {msg}")

    def _prepare(conn, query_id, is_master, configs):
        """prepare_query(), which also plans the query.  A query that fails
        there is recorded as failed for every config in configs."""
        query_def = queries_dict[query_id]
        try:
            prepare_query(conn, query_def["sql"], gucs=query_def.get("gucs", {}),
                          is_master=is_master, stmt_name=bench_stmt_name(query_id))
            return True
        except psycopg.Error as e:
            _record_error(conn, query_id, configs, e)
            return False

    def _capture(conn, query_id, config, is_master, prefetch_setting):
        query_def = queries_dict[query_id]
        try:
            plans[query_id][config] = capture_plan(conn, query_def, is_master,
                                                   prefetch_setting)
        except psycopg.Error as e:
            _record_error(conn, query_id, [config], e)

    # --- baseline ---
    print(f"\n{'=' * 60}")
    print(f"Capturing plans on BASELINE ({baseline_name})")
    print(f"{'=' * 60}")
    start_server(baseline_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
    try:
        _ensure_data(baseline_conn, baseline_name)
        conn = bench_connect(baseline_conn)
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
        for query_id in selected_queries:
            query_def = queries_dict[query_id]
            print(f"\n{query_id}: {query_def['name']}...")
            if _prepare(conn, query_id, True, ["master"]):
                _capture(conn, query_id, "master", True, None)
                deallocate_query(conn)
        conn.close()
    finally:
        stop_server(baseline_bin, baseline_data_dir)
        time.sleep(2)

    # --- testbranch ---
    print(f"\n{'=' * 60}")
    print(f"Capturing plans on TESTBRANCH ({testbranch_name})")
    print(f"{'=' * 60}")
    start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
    try:
        if not shared_data_dir:
            _ensure_data(test_conn, testbranch_name)
        conn = bench_connect(test_conn)
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
        test_has_prefetch = verify_test_has_prefetch(conn, test_has_prefetch, testbranch_name,
                                                     args.prefetch_disabled)
        test_is_master = not test_has_prefetch
        configs_run = {"patch_off": not args.prefetch_only and test_has_prefetch,
                       "patch_on": not args.prefetch_disabled}
        for query_id in selected_queries:
            query_def = queries_dict[query_id]
            if not _prepare(conn, query_id, test_is_master,
                            [c for c, ran in configs_run.items() if ran]):
                continue
            if configs_run["patch_off"]:
                print(f"\n{query_id}: {query_def['name']} (prefetch=off)...")
                _capture(conn, query_id, "patch_off", False, "off")
            if configs_run["patch_on"]:
                if test_has_prefetch:
                    print(f"\n{query_id}: {query_def['name']} (prefetch=on)...")
                    _capture(conn, query_id, "patch_on", False, "on")
                else:
                    print(f"\n{query_id}: {query_def['name']}...")
                    _capture(conn, query_id, "patch_on", True, None)
            deallocate_query(conn)
        conn.close()
    finally:
        stop_server(patch_bin, test_data_dir)
        time.sleep(2)

    # --- compare ---
    config_labels = {"patch_off": "prefetch=off", "patch_on": "prefetch=on"}
    mismatches = []
    n_checked = 0
    for query_id in selected_queries:
        base = plans[query_id].get("master")
        for config in (c for c, ran in configs_run.items() if ran):
            test = plans[query_id].get(config)
            if base is None or test is None:
                if (query_id, config) in errors or (query_id, "master") in errors:
                    mismatches.append({
                        "query_id": query_id, "config": config_labels[config],
                        "diffs": ["capture failed: "
                                  + (errors.get((query_id, "master"))
                                     or errors.get((query_id, config)))],
                    })
                continue
            n_checked += 1
            diffs = compare_plan_signatures(base, test)
            if diffs:
                mismatches.append({"query_id": query_id,
                                   "config": config_labels[config],
                                   "diffs": diffs})

    BOLD = "\033[1m"
    RESET = "\033[0m"
    print(f"\n{'=' * 60}")
    print(f"PLAN VERIFICATION: {testbranch_name} vs {baseline_name}")
    print(f"{'=' * 60}")
    print(f"  Checked: {n_checked} query/config pairs over {len(selected_queries)} queries")
    if not mismatches:
        print(f"  {BOLD}All plans and actual row counts match.{RESET}")
    else:
        print(f"  {BOLD}{len(mismatches)} MISMATCH(ES) -- needs debugging before "
              f"timings are comparable:{RESET}")
        for m in mismatches:
            query_id = m["query_id"]
            print(f"\n  {BOLD}{query_id} ({m['config']}): "
                  f"{queries_dict[query_id]['name']}{RESET}")
            for d in m["diffs"]:
                print(f"    - {d}")
            base = plans[query_id].get("master")
            config = next(k for k, v in config_labels.items() if v == m["config"])
            test = plans[query_id].get(config)
            if base is not None:
                print(f"    {baseline_name} plan:")
                for line in plan_signature_lines(base):
                    print(f"      {line}")
            if test is not None:
                print(f"    {testbranch_name} ({m['config']}) plan:")
                for line in plan_signature_lines(test):
                    print(f"      {line}")

    # Save the captured signatures + verdict alongside the benchmark results.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_file = os.path.join(OUTPUT_DIR, f"verify_{mode_name}_{timestamp}.json")
    with open(json_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "baseline": baseline_name,
            "testbranch": testbranch_name,
            "master_hash": master_hash,
            "patch_hash": patch_hash,
            "mode": mode_name,
            "queries": {qid: {"name": queries_dict[qid]["name"], "plans": plans[qid]}
                        for qid in selected_queries},
            "errors": {f"{q}/{c}": msg for (q, c), msg in errors.items()},
            "mismatches": mismatches,
        }, f, indent=2)
    print(f"\nPlan signatures saved to: {json_file}")
    return mismatches


def run_stress_test(args):
    """Run stress test mode: randomly generate queries to find regressions."""
    # Setup tmpfs with hugepages if requested
    tmpfs_mount = None
    baseline_name = getattr(args, "baseline", "master")
    baseline_label = baseline_name.upper()
    baseline_bin_orig, baseline_data_dir, baseline_source_dir, baseline_conn = BASELINE_CONFIGS[baseline_name]
    testbranch_name = getattr(args, "testbranch", "patch")
    test_bin_orig, test_data_dir, test_source_dir, test_conn = TESTBRANCH_CONFIGS[testbranch_name]
    test_has_prefetch = BUILD_HAS_PREFETCH[testbranch_name]
    master_bin = baseline_bin_orig
    patch_bin = test_bin_orig
    cleanup_tmpfs_now = lambda: None   # replaced once tmpfs exists; armed vs aborts
    nvme_awake = nvme_kept_awake()
    note_nvme_power_state(args, nvme_awake)

    if not args.no_tmpfs_hugepages:
        tmpfs_mount = setup_tmpfs_hugepages()
        cleanup_tmpfs_now = _arm_tmpfs_cleanup(tmpfs_mount)
        master_bin = copy_binaries_to_tmpfs(baseline_bin_orig, tmpfs_mount, "baseline")
        patch_bin = copy_binaries_to_tmpfs(test_bin_orig, tmpfs_mount, "testbranch")
        print(f"\nUsing tmpfs binaries:")
        print(f"  baseline ({baseline_name}): {master_bin}")
        print(f"  testbranch ({testbranch_name}): {patch_bin}\n")
    else:
        print("\nSkipping tmpfs hugepages setup (disabled with --no-tmpfs-hugepages)")

    # In cached mode, single-sample comparisons are too noisy for small
    # regressions.  Use multiple runs per query and compare representative
    # values (min or median) instead.  The batch loops measure through
    # measure_query() with this as the forced sample count, so a query gets
    # the benchmark path's full protocol -- warm-up until settled for cheap
    # queries (bounded by WARMUP_MAX and --warmup-budget, ~0.5 s worst case
    # per side; anything slower than WARMUP_MAX_QUERY_MS keeps its probe and
    # warms no further), the baseline's warm-up count replayed on the
    # testbranch, and the outlying-first-run replacement -- without the
    # dynamic run-count budgets, which would cost stress throughput.
    stress_runs = args.runs if args.runs is not None else (RUNS_MIN if args.cached else 1)

    # Novelty bias: the shapes the suites already measure plus any
    # --stress-avoid extras; see STRESS_KNOWN_BAD.
    avoid = list(STRESS_KNOWN_BAD) + args.stress_avoid_entries
    avoid_base = args.stress_avoid_known
    avoid_stats = Counter()

    print("=" * 60)
    print("STRESS TEST MODE")
    print("=" * 60)
    print(f"Looking for regressions >= {STRESS_REGRESSION_THRESHOLD - 1:.0%} slower than {baseline_name}")
    print(f"Generating {STRESS_QUERIES_PER_BATCH} queries per batch")
    print(f"Runs per query: {stress_runs}"
          + (" (--runs)" if args.runs is not None else " (cached mode)" if args.cached else ""))
    print(f"Minimum query duration: {args.min_query_ms:.1f} ms")
    print(f"Novelty bias: {describe_stress_avoid(avoid, avoid_base, len(args.stress_avoid_entries))}")
    mode_word = ("\033[32m💵  cached\033[0m" if args.cached
                 else "\033[36m🧊  uncached, cold indexes (--no-cached-index)\033[0m"
                 if args.no_cached_index
                 else "\033[34m💾  uncached\033[0m")
    print(f"Mode: {mode_word}")
    print(f"Backend pinning: {pinning_summary(args)}")
    print(f"Timing method: {timing_method_label(args)}")
    print("=" * 60)

    # Get git hashes
    master_hash = get_git_hash(baseline_source_dir)
    patch_hash = get_git_hash(test_source_dir)
    print(f"\nBaseline ({baseline_name}) git hash: {master_hash}")
    print(f"Patch git hash: {patch_hash}")
    print_io_settings(args)

    # We assume data is already loaded (--skip-load behavior for stress test)
    # User should run the normal benchmark first to ensure data exists

    iteration = 0
    total_queries_tested = 0

    try:
        while True:
            iteration += 1
            print(f"\n{'=' * 60}")
            print(f"ITERATION {iteration}")
            print(f"{'=' * 60}")

            # Generate batch of random queries
            queries = []
            for i in range(STRESS_QUERIES_PER_BATCH):
                query_num = total_queries_tested + i + 1
                queries.append((f"S{query_num}",
                                generate_random_query(query_num, avoid, avoid_base, avoid_stats)))

            # Print generated queries
            print(f"\nGenerated {len(queries)} queries:")
            for query_id, query_def in queries:
                print(f"  {query_id}: {query_def['name']}")

            # Results storage for this batch
            results = {}
            for query_id, query_def in queries:
                results[query_id] = {
                    "query_def": query_def,
                    "master": {"times": [], "min": None, "explain": None,
                               "warmup_runs": None},
                    "patch_off": {"times": [], "min": None, "explain": None},
                    "patch_on": {"times": [], "min": None, "explain": None},
                }

            # Run all queries on baseline, replacing any that are too fast
            print(f"\n--- Running on BASELINE ({baseline_name}) ---")
            start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
            try:
                master_conn = bench_connect(baseline_conn)
                pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                with master_conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

                # Process each query slot, replacing too-fast queries
                for i in range(len(queries)):
                    query_id, query_def = queries[i]
                    replacement_count = 0

                    while True:
                        print(f"\n  {query_id}: {query_def['name']}")
                        # Print query text
                        for line in query_def['sql'].strip().split('\n'):
                            print(f"    {line.strip()}")
                        print(f"  Running...", flush=True)
                        try:
                            plan = prepare_query(master_conn, query_def["sql"],
                                                 gucs=query_def.get("gucs", {}), is_master=True,
                                                 stmt_name=bench_stmt_name(query_id))
                            # Measure exactly like the benchmark path: warm-up
                            # until the query settles (cheap queries only; the
                            # probe of anything over WARMUP_MAX_QUERY_MS is
                            # kept as sample 1), then stress_runs timed runs
                            # with the outlying-first-run replacement.  A
                            # too-fast draw wastes only runs of a sub-
                            # min_query_ms query on this.
                            times, _explains, warmup_runs = measure_query(
                                master_conn, query_def, args, is_master=True,
                                prefetch_setting=None, io_supported=False,
                                on_patch=False, force_count=stress_runs)
                            deallocate_query(master_conn)
                            rep = get_representative(times)
                            if rep is None:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                            if rep < args.min_query_ms:
                                replacement_count += 1
                                print(f"  → {rep:.3f} ms (too fast, regenerating...)")
                                # Generate a replacement query with same ID
                                query_num = int(query_id[1:])  # Extract number from "S123"
                                query_def = generate_random_query(
                                    query_num + replacement_count * 1000,
                                    avoid, avoid_base, avoid_stats)
                                queries[i] = (query_id, query_def)
                                # Update results dict for new query
                                results[query_id] = {
                                    "query_def": query_def,
                                    "master": {"times": [], "min": None, "explain": None,
                                               "warmup_runs": None},
                                    "patch_off": {"times": [], "min": None, "explain": None},
                                    "patch_on": {"times": [], "min": None, "explain": None},
                                }
                                continue  # Try again with new query
                            results[query_id]["master"]["times"] = times
                            results[query_id]["master"]["explain"] = plan
                            results[query_id]["master"]["warmup_runs"] = warmup_runs
                            results[query_id]["master"]["min"] = rep
                            print(f"  → {rep:.3f} ms")
                            break  # Query is acceptable, move to next slot
                        except psycopg.Error as e:
                            stress_query_failed(e, query_def)

                master_conn.close()
            finally:
                stop_server(master_bin, baseline_data_dir)
                time.sleep(2)

            # Run all queries on testbranch
            print(f"\n--- Running on TESTBRANCH ({testbranch_name}) ---")
            start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
            try:
                patch_conn = bench_connect(test_conn)
                pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                with patch_conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

                # Tolerate a testbranch build that lacks the prefetch GUC (warns
                # + treats it as a baseline).  Once downgraded it stays False
                # across later batches and the re-confirm phases.
                test_has_prefetch = verify_test_has_prefetch(
                    patch_conn, test_has_prefetch, testbranch_name, args.prefetch_disabled)

                BOLD = "\033[1m"
                RESET = "\033[0m"
                test_is_master = not test_has_prefetch

                for query_id, query_def in queries:
                    master_min = results[query_id]["master"]["min"]

                    print(f"\n  {query_id}: {query_def['name']}")
                    # Print query text
                    for line in query_def['sql'].strip().split('\n'):
                        print(f"    {line.strip()}")

                    plan = prepare_query(patch_conn, query_def["sql"],
                                         gucs=query_def.get("gucs", {}), is_master=test_is_master,
                                         stmt_name=bench_stmt_name(query_id))

                    # Match the baseline's sample and warm-up run counts, the
                    # way the benchmark's testbranch phase does, so both
                    # sides' timed samples start at the same execution number.
                    master_n = len(results[query_id]["master"]["times"])
                    patch_count = master_n if master_n > 0 else stress_runs
                    patch_warmup = (results[query_id]["master"]["warmup_runs"]
                                    if master_n > 0 else None)

                    # Test with prefetch OFF (skip if --prefetch-only or no prefetch GUC)
                    if not args.prefetch_only and test_has_prefetch:
                        print(f"  prefetch=off...", flush=True)
                        try:
                            times, _explains, _w = measure_query(
                                patch_conn, query_def, args, is_master=False,
                                prefetch_setting="off", io_supported=False,
                                on_patch=True, force_count=patch_count,
                                force_warmup=patch_warmup)
                            results[query_id]["patch_off"]["times"] = times
                            results[query_id]["patch_off"]["explain"] = plan
                            patch_off_rep = get_representative(times)
                            results[query_id]["patch_off"]["min"] = patch_off_rep
                            if patch_off_rep is None:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                            if master_min:
                                ratio = patch_off_rep / master_min
                                print(f"  → {patch_off_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                            else:
                                print(f"  → {patch_off_rep:.3f} ms")
                        except psycopg.Error as e:
                            stress_query_failed(e, query_def)

                    # Test with prefetch ON (skip if --prefetch-disabled)
                    # When testbranch has no prefetch GUC, run queries plain
                    if not args.prefetch_disabled:
                        if test_has_prefetch:
                            print(f"  prefetch=on...", flush=True)
                            pf_setting = "on"
                        else:
                            print(f"  running...", flush=True)
                            pf_setting = None
                        try:
                            times, _explains, _w = measure_query(
                                patch_conn, query_def, args,
                                is_master=test_is_master,
                                prefetch_setting=pf_setting, io_supported=False,
                                on_patch=True, force_count=patch_count,
                                force_warmup=patch_warmup)
                            results[query_id]["patch_on"]["times"] = times
                            results[query_id]["patch_on"]["explain"] = plan
                            patch_on_rep = get_representative(times)
                            results[query_id]["patch_on"]["min"] = patch_on_rep
                            if patch_on_rep is None:
                                print("FAILED - could not extract execution time")
                                sys.exit(1)
                            if master_min:
                                ratio = patch_on_rep / master_min
                                print(f"  → {patch_on_rep:.3f} ms ({BOLD}{ratio:.3f}x{RESET} vs baseline)")
                            else:
                                print(f"  → {patch_on_rep:.3f} ms")
                        except psycopg.Error as e:
                            stress_query_failed(e, query_def)

                    deallocate_query(patch_conn)

                patch_conn.close()
            finally:
                stop_server(patch_bin, test_data_dir)
                time.sleep(2)

            # Check for regressions
            print(f"\n--- Checking for regressions ---")
            regressions_found = []

            for query_id, query_def in queries:
                r = results[query_id]
                master_min = r["master"]["min"]
                patch_off_min = r["patch_off"]["min"]
                patch_on_min = r["patch_on"]["min"]

                if master_min is None:
                    continue

                # Check prefetch=off regression
                if patch_off_min is not None:
                    ratio_off = patch_off_min / master_min
                    if ratio_off >= STRESS_REGRESSION_THRESHOLD:
                        regressions_found.append({
                            "query_id": query_id,
                            "query_def": query_def,
                            "config": "prefetch=off",
                            "ratio": ratio_off,
                            "master_ms": master_min,
                            "patch_ms": patch_off_min,
                            "patch_on_ms": patch_on_min,
                        })

                # Check prefetch=on regression
                if patch_on_min is not None:
                    ratio_on = patch_on_min / master_min
                    if ratio_on >= STRESS_REGRESSION_THRESHOLD:
                        regressions_found.append({
                            "query_id": query_id,
                            "query_def": query_def,
                            "config": "prefetch=on",
                            "ratio": ratio_on,
                            "master_ms": master_min,
                            "patch_ms": patch_on_min,
                            "patch_off_ms": patch_off_min,
                        })

            total_queries_tested += len(queries)

            # Verify apparent regressions with retries to filter out spurious failures
            if regressions_found:
                print(f"\n--- Verifying {len(regressions_found)} apparent regression(s) with retries ---")
                confirmed_regressions = []
                needs_rebaseline = []  # Regressions that survived retries, need master re-baseline

                start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
                try:
                    patch_conn = bench_connect(test_conn)
                    pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                    with patch_conn.cursor() as cur:
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

                    for reg in regressions_found:
                        query_def = reg["query_def"]
                        master_avg = reg["master_ms"]
                        if test_has_prefetch:
                            prefetch_setting = "off" if reg["config"] == "prefetch=off" else "on"
                        else:
                            prefetch_setting = None

                        print(f"\n  Verifying {reg['query_id']} ({reg['config']}, initial {reg['ratio']:.3f}x)...")

                        prepare_query(patch_conn, query_def["sql"],
                                      gucs=query_def.get("gucs", {}),
                                      is_master=test_is_master, prefetch_setting=prefetch_setting,
                                      stmt_name=bench_stmt_name(reg["query_id"]))

                        # Simple retries: 3 attempts with short delay.  These
                        # (and the re-baseline pass below) only gate whether a
                        # regression survives; they stay on plain fixed-count
                        # run_query() loops -- the primary numbers above use
                        # measure_query() and the benchmark protocol.
                        RETRY_COUNT = 3
                        RETRY_DELAY = 1.0

                        regression_confirmed = True
                        for retry in range(RETRY_COUNT):
                            print(f"    Retry {retry + 1}/{RETRY_COUNT}...", end=" ", flush=True)
                            time.sleep(RETRY_DELAY)

                            retry_times = []
                            for run_i in range(stress_runs):
                                exec_time, _ = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=test_is_master, prefetch_setting=prefetch_setting,
                                    benchmark_cpu=args.benchmark_cpu,
                                    serialize=args.serialize,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if exec_time is not None:
                                    retry_times.append(exec_time)

                            if retry_times:
                                retry_rep = get_representative(retry_times)
                                ratio = retry_rep / master_avg
                                if stress_runs > 1:
                                    print(f"{' '.join(f'{t:.3f}' for t in retry_times)} → {retry_rep:.3f} ms ({ratio:.3f}x)")
                                else:
                                    print(f"{retry_rep:.3f} ms ({ratio:.3f}x)")

                                if ratio < STRESS_REGRESSION_THRESHOLD:
                                    print(f"    Under threshold - spurious failure, discarding")
                                    regression_confirmed = False
                                    break
                            else:
                                print("FAILED")

                        deallocate_query(patch_conn)

                        if regression_confirmed:
                            print(f"    Retries confirm regression - needs {baseline_name} re-baseline")
                            needs_rebaseline.append(reg)

                    patch_conn.close()
                finally:
                    stop_server(patch_bin, test_data_dir)
                    time.sleep(2)

                # Phase 2: Re-baseline verification for regressions that survived retries
                # This accounts for environmental drift since the original master baseline
                if needs_rebaseline:
                    print(f"\n--- Re-baselining {len(needs_rebaseline)} regression(s) against {baseline_name} ---")

                    for reg in needs_rebaseline:
                        query_def = reg["query_def"]
                        prefetch_setting = "off" if reg["config"] == "prefetch=off" else "on"

                        print(f"\n  Re-baselining {reg['query_id']} ({reg['config']})...")

                        # Run query on baseline to establish new baseline
                        start_server(master_bin, baseline_name, baseline_data_dir, baseline_conn, args, on_patch=False)
                        try:
                            master_conn = bench_connect(baseline_conn)
                            pin_backend(master_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                            with master_conn.cursor() as cur:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

                            prepare_query(master_conn, query_def["sql"],
                                          gucs=query_def.get("gucs", {}), is_master=True,
                                          stmt_name=bench_stmt_name(reg["query_id"]))
                            rebaseline_master_times = []
                            for run_i in range(stress_runs):
                                t, _ = run_query(
                                    master_conn, query_def, args.cached,
                                    is_master=True, prefetch_setting=None,
                                    benchmark_cpu=args.benchmark_cpu,
                                    serialize=args.serialize,
                                    direct_io=server_direct_io(args, on_patch=False),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if t is not None:
                                    rebaseline_master_times.append(t)
                            deallocate_query(master_conn)
                            master_conn.close()
                        finally:
                            stop_server(master_bin, baseline_data_dir)
                            time.sleep(2)

                        if not rebaseline_master_times:
                            print(f"    Baseline re-baseline FAILED, discarding regression")
                            continue

                        new_master_time = get_representative(rebaseline_master_times)
                        best_master_time = min(new_master_time, reg["master_ms"])
                        if stress_runs > 1:
                            print(f"    New {baseline_name} baseline: {' '.join(f'{t:.3f}' for t in rebaseline_master_times)} → {new_master_time:.3f} ms (was {reg['master_ms']:.3f} ms, using {best_master_time:.3f} ms)")
                        else:
                            print(f"    New {baseline_name} baseline: {new_master_time:.3f} ms (was {reg['master_ms']:.3f} ms, using {best_master_time:.3f} ms)")

                        # Run query on testbranch with new baseline
                        start_server(patch_bin, testbranch_name, test_data_dir, test_conn, args, on_patch=True)
                        try:
                            patch_conn = bench_connect(test_conn)
                            pin_backend(patch_conn.info.backend_pid, args.benchmark_cpu, enabled=args.pin)
                            with patch_conn.cursor() as cur:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

                            prepare_query(patch_conn, query_def["sql"],
                                          gucs=query_def.get("gucs", {}),
                                          is_master=test_is_master, prefetch_setting=prefetch_setting,
                                          stmt_name=bench_stmt_name(reg["query_id"]))
                            rebaseline_patch_times = []
                            for run_i in range(stress_runs):
                                t, _ = run_query(
                                    patch_conn, query_def, args.cached,
                                    is_master=test_is_master, prefetch_setting=prefetch_setting,
                                    benchmark_cpu=args.benchmark_cpu,
                                    serialize=args.serialize,
                                    direct_io=server_direct_io(args, on_patch=True),
                                    skip_prewarm=(args.cached and run_i > 0)
                                )
                                if t is not None:
                                    rebaseline_patch_times.append(t)
                            deallocate_query(patch_conn)
                            patch_conn.close()
                        finally:
                            stop_server(patch_bin, test_data_dir)
                            time.sleep(2)

                        if not rebaseline_patch_times:
                            print(f"    Patch re-run FAILED, discarding regression")
                            continue

                        new_patch_time = get_representative(rebaseline_patch_times)
                        best_patch_time = min(new_patch_time, reg["patch_ms"])
                        best_ratio = best_patch_time / best_master_time
                        if stress_runs > 1:
                            print(f"    New patch time: {' '.join(f'{t:.3f}' for t in rebaseline_patch_times)} → {new_patch_time:.3f} ms (was {reg['patch_ms']:.3f} ms, using {best_patch_time:.3f} ms)")
                        else:
                            print(f"    New patch time: {new_patch_time:.3f} ms (was {reg['patch_ms']:.3f} ms, using {best_patch_time:.3f} ms)")
                        print(f"    Best ratio: {best_ratio:.3f}x (orig {reg['ratio']:.3f}x, rebaseline {new_patch_time / new_master_time:.3f}x)")

                        if best_ratio >= STRESS_REGRESSION_THRESHOLD:
                            print(f"    Regression CONFIRMED with fresh baseline")
                            # Update reg with best measurements from either run
                            reg["master_ms"] = best_master_time
                            reg["patch_ms"] = best_patch_time
                            reg["ratio"] = best_ratio
                            confirmed_regressions.append(reg)
                        else:
                            print(f"    Below threshold - discarding (orig {reg['ratio']:.3f}x, rebaseline {new_patch_time / new_master_time:.3f}x, best {best_ratio:.3f}x)")

                regressions_found = confirmed_regressions

            if regressions_found:
                # Sort by ratio (worst first)
                regressions_found.sort(key=lambda x: x["ratio"], reverse=True)
                worst = regressions_found[0]

                failure_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print()
                print("=" * 60)
                print("REGRESSION FOUND!")
                print(f"Time: {failure_time}")
                print("=" * 60)
                pct = (worst["ratio"] - 1) * 100
                print(f"Patch ({worst['config']}) is {worst['ratio']:.3f}x slower than {baseline_name} ({pct:.1f}% regression)")
                print()
                print(f"{baseline_label + ':':18s}{worst['master_ms']:.3f} ms")

                query_def = worst["query_def"]
                r = results[worst["query_id"]]

                # The regressed config shows its re-measured time, like the
                # headline; the other config was not re-measured.
                for cfg, label in (("patch_off", "off"), ("patch_on", "on")):
                    regressed = worst["config"] == f"prefetch={label}"
                    patch_ms = worst["patch_ms"] if regressed else r[cfg]["min"]
                    if patch_ms:
                        note = " <-- REGRESSION" if regressed else " (not re-measured)"
                        print(f"{'Patch (' + label + '):':18s}{patch_ms:.3f} ms "
                              f"({patch_ms / worst['master_ms']:.3f}x vs {baseline_name}){note}")

                # Print the plans prepare_query() captured (the stress test
                # never runs EXPLAIN ANALYZE)
                print()
                print("Plans (EXPLAIN of the prepared statement; not EXPLAIN ANALYZE):")
                print()
                if r["master"]["explain"]:
                    print(f"  {baseline_name}:")
                    for line in r["master"]["explain"].split('\n'):
                        print(f"    {line}")
                if r["patch_off"]["explain"]:
                    print()
                    print("  patch (prefetch=off):")
                    for line in r["patch_off"]["explain"].split('\n'):
                        print(f"    {line}")
                if r["patch_on"]["explain"]:
                    print()
                    print("  patch (prefetch=on):")
                    for line in r["patch_on"]["explain"].split('\n'):
                        print(f"    {line}")

                # Print query definition ready to paste into a suite .toml file
                print()
                print("Add this to a suite's .toml file (e.g. suites/00-benchmark.toml):")
                print()

                # Format evict / prewarm lists as TOML arrays of strings
                evict_str = ", ".join(f'"{t}"' for t in query_def["evict"])
                prewarm_idx_str = ", ".join(f'"{i}"' for i in query_def["prewarm_indexes"])
                prewarm_tbl_str = ", ".join(f'"{t}"' for t in query_def["prewarm_tables"])

                # Clean up SQL formatting
                sql_lines = query_def["sql"].strip().split('\n')
                sql_formatted = '\n'.join('            ' + line.strip() for line in sql_lines)

                print(f'[queries.STRESS_{total_queries_tested}]')
                print(f'name = "{query_def["name"]}"')
                print("sql = '''")
                print(sql_formatted)
                print("'''")
                print(f'evict = [{evict_str}]')
                print(f'prewarm_indexes = [{prewarm_idx_str}]')
                print(f'prewarm_tables = [{prewarm_tbl_str}]')
                if query_def.get("gucs"):
                    gucs_str = ", ".join(f'"{k}" = "{v}"' for k, v in query_def["gucs"].items())
                    print(f'gucs = {{ {gucs_str} }}')
                print()
                print(f"Total queries tested: {total_queries_tested}")
                print(f"Iterations: {iteration}")
                print_stress_avoid_stats(avoid_stats)
                return  # Stop on confirmed regression

            else:
                print(f"No regressions found in this batch.")
                print(f"Total queries tested so far: {total_queries_tested}")
                if avoid_base > 0.0:
                    print(f"Novelty bias: {stress_avoid_redraws(avoid_stats)} candidate(s) redrawn so far")
                print("Generating next batch...")

    except KeyboardInterrupt:
        print(f"\n\nStress test interrupted after {total_queries_tested} queries ({iteration} iterations)")
        print("No regressions found.")
        print_stress_avoid_stats(avoid_stats)
    finally:
        # Cleanup tmpfs if it was created (also armed via atexit against aborts)
        cleanup_tmpfs_now()


def _format_duration(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins == 0:
        return f"{secs} seconds"
    elif mins == 1:
        return f"1 minute {secs} seconds"
    else:
        return f"{mins} minutes {secs} seconds"


def _print_wall_time(wall_start):
    wall_end = time.time()
    start_dt = datetime.fromtimestamp(wall_start)
    end_dt = datetime.fromtimestamp(wall_end)
    print(f"\n{'=' * 60}")
    print(f"Started:       {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Finished:      {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total runtime: {_format_duration(wall_end - wall_start)}")
    print(f"{'=' * 60}")

    # Re-surface the missing-prefetch-GUC warning as the very last output so it
    # isn't buried in the scrollback of a long run.
    if MISSING_PREFETCH_GUC_TESTBRANCH is not None:
        print_missing_prefetch_guc_warning(MISSING_PREFETCH_GUC_TESTBRANCH)
    if NVME_ASLEEP_WARNING is not None:
        print_nvme_asleep_warning(NVME_ASLEEP_WARNING)


def resolve_selection(args):
    """The suites a run covers, as [(suite, query ids or None)], for main()
    and run_verify_mode() alike.

    --queries is authoritative and may span several suites: each requested
    id goes to its owning suite, and each involved suite runs just its
    subset, in the order of its first appearance in the list.  This
    decouples query selection from the suite flags, so `--queries WR1,Q1`
    works without `--worker-regress-feb`.  Without --queries, every suite
    whose flag was given runs all of its queries (None), in
    BENCHMARK_SUITES order; with no flag, the default suite does.
    """
    if not args.queries:
        flagged = [s for s in BENCHMARK_SUITES[1:]
                   if s["cli_dest"] and getattr(args, s["cli_dest"], False)]
        return [(s, None) for s in flagged] or [(BENCHMARK_SUITES[0], None)]

    suite_map = build_query_suite_map()
    # Drop repeated ids.  A repeated id ran twice on each side, the
    # testbranch matching the baseline's doubled sample count each time, so
    # it got twice the baseline's samples (a lower min) and counted twice in
    # the geomean.
    requested = list(dict.fromkeys(q.strip().upper() for q in args.queries.split(",")
                                   if q.strip()))
    unknown = [q for q in requested if q not in suite_map]
    if unknown:
        all_ids = [qid for s in BENCHMARK_SUITES for qid in s["queries"].keys()]
        print(f"Error: Unknown query {', '.join(unknown)}. "
              f"Available: {', '.join(all_ids)}")
        sys.exit(1)

    # Group requested ids by owning suite, preserving each suite's first
    # appearance in the --queries list; within a suite, keep listed order.
    grouped = OrderedDict()  # mode_prefix -> (suite, [qids])
    for qid in requested:
        suite = suite_map[qid]
        grouped.setdefault(suite["mode_prefix"], (suite, []))[1].append(qid)
    return list(grouped.values())


def print_suite_banner(suite, qids):
    """Header for one suite of a run that covers several."""
    print(f"\n{'#' * 60}")
    print(f"# Suite: {suite['mode_prefix']}"
          + (f"  ({', '.join(qids)})" if qids is not None else ""))
    print(f"{'#' * 60}")


def run_verify_mode(args):
    """--verify driver: resolve the same suite/query selection main() uses for
    a benchmark run, verify each involved suite, and exit 1 on any mismatch."""
    selection = resolve_selection(args)

    all_mismatches = []
    for suite, qids in selection:
        if len(selection) > 1:
            print_suite_banner(suite, qids)
        mode_name = suite["mode_prefix"]
        if args.bitmap:
            mode_name = f"{mode_name}_bitmap"
        all_mismatches.extend(run_verify(
            args, suite["queries"], mode_name, suite["title"],
            suite["verify_fn"], suite["load_fn"], query_ids=qids))

    if len(selection) > 1:
        print(f"\n{'#' * 60}")
        if all_mismatches:
            print(f"# {len(all_mismatches)} plan mismatch(es) across all suites: "
                  + ", ".join(f"{m['query_id']} ({m['config']})" for m in all_mismatches))
        else:
            print("# All suites: plans and actual row counts match")
        print(f"{'#' * 60}")
    if all_mismatches:
        sys.exit(1)


def main():
    global USE_MEDIAN, GLOBAL_DROP_CACHES, NO_CACHED_INDEX
    wall_start = time.time()
    args = parse_arguments()
    args = resolve_io_method_defaults(args)

    # Set global median flag
    USE_MEDIAN = args.use_median
    GLOBAL_DROP_CACHES = getattr(args, "global_drop_caches", False)
    NO_CACHED_INDEX = args.no_cached_index

    # Update data dirs if using delay pgdata
    if args.delay_pgdata:
        # Update baseline config
        base_bin, _base_data, base_src, base_conn = BASELINE_CONFIGS[args.baseline]
        base_dir = os.path.dirname(_base_data)
        base_delay = os.path.join(base_dir, "data-delay")
        BASELINE_CONFIGS[args.baseline] = (base_bin, base_delay, base_src, base_conn)
        # Update testbranch config
        test_bin, _test_data, test_src, test_conn = TESTBRANCH_CONFIGS[args.testbranch]
        test_dir = os.path.dirname(_test_data)
        test_delay = os.path.join(test_dir, "data-delay")
        TESTBRANCH_CONFIGS[args.testbranch] = (test_bin, test_delay, test_src, test_conn)
        print(f"Using delayed I/O data directories:")
        print(f"  Baseline ({args.baseline}): {base_delay}")
        print(f"  Testbranch ({args.testbranch}): {test_delay}")

    if args.list_modes:
        modes_json = []
        for suite in BENCHMARK_SUITES:
            modes_json.append({
                "mode_prefix": suite["mode_prefix"],
                "cli_flag": suite["cli_flag"],
                "uncached_runs": suite["uncached_runs"],
                "cached_runs": suite["cached_runs"],
            })
        print(json.dumps(modes_json))
        return

    # Check every server binary against its data directory before anything
    # else touches the machine (interferer kill, tmpfs, data load): a stale
    # install -- master not rebuilt across a catversion bump, say -- fails
    # here, at once.
    preflight_servers(args)

    # --verify times nothing, so the machine-quieting steps below (killing
    # interferers, THP / governor checks) are skipped: only the plans matter.
    if args.verify:
        run_verify_mode(args)
        _print_wall_time(wall_start)
        return

    # Stop background processes (vscode-server, claude) that would skew
    # results.  Interactive: prompts (answer n to keep them running while
    # debugging); non-interactive: --force.  Done after metadata-only modes
    # like --list-modes so they don't disturb the machine.
    if args.no_kill_interferers:
        print("Skipping kill_vscode_server.sh (--no-kill-interferers); "
              "interfering processes may add measurement noise.")
    else:
        kill_interfering_processes()

    # Verify THP / CPU governor are benchmark-friendly (warns + prompts, aborts
    # if declined).  Same placement rationale as the kill above.
    check_benchmark_env()

    # Pin this process before any timed work; every run mode below goes
    # through the same connection-and-drain code this stabilizes.
    pin_client(args)

    if args.stress_test:
        run_stress_test(args)
        _print_wall_time(wall_start)
        return

    # --no-cached-index gets its own mode namespace, and the tag is prefixed
    # rather than suffixed on purpose: result files are picked by mode-name
    # prefix, so "<suite>_uncached_noidx_<timestamp>" files would match the
    # plain "<suite>_uncached_" lookup and cold-index numbers could pass for
    # warm-index ones.
    cache_tag = ("cached" if args.cached
                 else "noidx_uncached" if args.no_cached_index
                 else "uncached")
    # --bitmap gets its own mode namespace: patch_report.py and
    # --old-master-results pick result files by mode-name prefix, and
    # bitmap-forced numbers must never pass for index-scan ones.
    if args.bitmap:
        cache_tag = f"bitmap_{cache_tag}"

    selection = resolve_selection(args)
    multi = len(selection) > 1
    combined_ratios = []
    for suite, qids in selection:
        if multi:
            print_suite_banner(suite, qids)
        suite_ratios = run_generic_benchmark(
            args, suite["queries"],
            f"{suite['mode_prefix']}_{cache_tag}",
            suite["title"],
            suite["verify_fn"], suite["load_fn"],
            sync_stats=suite.get("sync_stats", False),
            tables=suite.get("tables"),
            expect_all_visible=suite.get("expect_all_visible", True),
            query_ids=qids)
        if suite_ratios:
            combined_ratios.extend(suite_ratios)

    # When the run spanned several suites, pool every query into one
    # ranking + geomean so the cross-suite result reads as a single set.
    # Query ids are globally unique, so there are no collisions to resolve.
    if multi and combined_ratios:
        baseline_name = getattr(args, "baseline", "master")
        baseline_label = baseline_name.upper()
        print(f"\n{'#' * 60}")
        print(f"# COMBINED RESULTS ACROSS ALL SUITES "
              f"({len({e['query_id'] for e in combined_ratios})} queries)")
        print(f"{'#' * 60}")
        print_ratio_rankings(combined_ratios, args, baseline_name,
                             baseline_label, scope="ALL SUITES — ")

    _print_wall_time(wall_start)


if __name__ == "__main__":
    main()
