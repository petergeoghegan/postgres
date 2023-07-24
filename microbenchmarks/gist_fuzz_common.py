#!/usr/bin/env python3
"""
Shared infrastructure for the GiST amgetbatch adversarial stress tools.

The patch branch (index-prefetch-v0.49) replaces GiST's old tuple-at-a-time
amgettuple path with a batch-oriented amgetbatch path (commit cc41629c).  Master
still has the old path, so master is used as a correctness oracle.

This module provides everything both tools share:

  * Server lifecycle for two purpose-built throwaway clusters (fresh initdb on a
    scratch root -- never the persistent regression DBs).
  * The opclass catalog: per-opclass column type, deterministic data generator,
    WHERE / ORDER BY operator templates, and capability flags.
  * EXPLAIN gating: force a scan type via enable_* GUCs and confirm via
    EXPLAIN (FORMAT JSON) that the plan really uses that scan type on our GiST
    index -- so a query that silently falls back to a seq scan cannot pass.
  * Result comparators: sorted-multiset equality for unordered/bitmap/IOS,
    exact distance-sequence equality with benign within-tie reordering for
    ordered (kNN) scans.
  * Repro dumping and crash / timeout detection.

Oracle  = master release build   (no asserts needed; fast; runs standalone).
DUT     = patch  cassert build   (asserts ON -- the whole point).
Both are PG 19beta1 so a deterministic seeded load yields identical data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import psycopg

# --------------------------------------------------------------------------
# Binaries and locations
# --------------------------------------------------------------------------

# Oracle: plain master, RELEASE build (no assertions, faster, runs standalone).
ORACLE_BIN = "/mnt/nvme/postgresql/master/install_meson_rc/bin"
# DUT: patch branch, cassert (debug) build -- we want assertions to fire.
DUT_BIN = "/mnt/nvme/postgresql/patch/install_meson_dc/bin"
# Valgrind build of the patch (optional --valgrind mode).
DUT_VALGRIND_BIN = "/mnt/nvme/postgresql/patch/install_meson_vc/bin"

# Throwaway clusters live here.  NEVER the persistent regression DBs.
SCRATCH_ROOT = "/mnt/nvme/postgresql/scratch/gistfuzz"

# Data dirs we must refuse to touch (the 79GB persistent regression clusters).
FORBIDDEN_DATA_DIRS = [
    "/mnt/nvme/postgresql/patch/data",
    "/mnt/nvme/postgresql/master/data",
    "/mnt/nvme/postgresql/REL_18_STABLE/data",
]

DB_NAME = "gistfuzz"
DB_USER = "pg"
SOCKET_DIR = "/tmp"

# Contrib extensions the catalog/tools need (installed in both builds).
# pg_buffercache provides pg_buffercache_evict_relation(), used to evict the heap
# from shared buffers so index-scan heap fetches actually run through the
# prefetch read stream (otherwise everything is cached and prefetch is a no-op).
REQUIRED_EXTENSIONS = ["btree_gist", "cube", "pg_trgm", "seg", "pageinspect",
                       "pg_buffercache"]


@dataclass
class Cluster:
    """A throwaway PostgreSQL cluster managed by the test harness."""
    role: str           # "oracle" or "dut"
    bin_dir: str
    port: int
    data_dir: str
    logfile: str
    valgrind: bool = False

    def conn_params(self, dbname: str = DB_NAME) -> dict:
        return {
            "dbname": dbname,
            "user": DB_USER,
            "host": SOCKET_DIR,
            "port": self.port,
        }


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

def _assert_safe_data_dir(data_dir: str) -> None:
    real = os.path.realpath(data_dir)
    for forbidden in FORBIDDEN_DATA_DIRS:
        if real == os.path.realpath(forbidden) or real.startswith(
            os.path.realpath(forbidden) + os.sep
        ):
            raise RuntimeError(
                f"REFUSING to use data dir {real}: it is (under) the persistent "
                f"regression cluster {forbidden}"
            )
    if not real.startswith(os.path.realpath(SCRATCH_ROOT) + os.sep):
        raise RuntimeError(
            f"REFUSING to use data dir {real}: not under scratch root "
            f"{SCRATCH_ROOT}"
        )


def make_cluster(role: str, bin_dir: str, port: int,
                 valgrind: bool = False) -> Cluster:
    data_dir = os.path.join(SCRATCH_ROOT, role)
    logfile = os.path.join(SCRATCH_ROOT, f"{role}.log")
    _assert_safe_data_dir(data_dir)
    return Cluster(role=role, bin_dir=bin_dir, port=port,
                   data_dir=data_dir, logfile=logfile, valgrind=valgrind)


def _run(cmd: List[str], env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def stop_cluster(c: Cluster) -> None:
    pg_ctl = os.path.join(c.bin_dir, "pg_ctl")
    if os.path.isdir(c.data_dir):
        _run([pg_ctl, "stop", "-D", c.data_dir, "-m", "immediate"])
        time.sleep(0.5)


def initdb_fresh(c: Cluster) -> None:
    """Wipe and re-create the cluster's data dir from scratch."""
    _assert_safe_data_dir(c.data_dir)
    stop_cluster(c)
    if os.path.isdir(c.data_dir):
        shutil.rmtree(c.data_dir)
    os.makedirs(os.path.dirname(c.data_dir), exist_ok=True)
    initdb = os.path.join(c.bin_dir, "initdb")
    res = _run([initdb, "-D", c.data_dir, "-U", DB_USER, "--no-sync",
                "--auth=trust", "-E", "UTF8", "--locale=C"])
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        raise RuntimeError(f"initdb failed for {c.role}")


# Server-side GUCs applied at startup.  io_method is overridable so the
# concurrency tool can request io_uring to hunt the known deadlock class.
def start_cluster(c: Cluster, extra_opts: Optional[List[str]] = None) -> None:
    pg_ctl = os.path.join(c.bin_dir, "pg_ctl")
    opts = [
        f"-p {c.port}",
        f"-k {SOCKET_DIR}",
        "--listen_addresses=localhost",
        "--autovacuum=off",
        "--fsync=off",
        "--max_parallel_workers_per_gather=0",
        # Two clusters run at once; keep shared memory small and never demand
        # huge pages (which may be unavailable / reserved on this host).
        "--shared_buffers=128MB",
        "--maintenance_work_mem=128MB",
        "--huge_pages=off",
        "--max_connections=50",
    ]
    if extra_opts:
        opts.extend(extra_opts)
    cmd = [pg_ctl, "start", "-D", c.data_dir, "-l", c.logfile, "-w",
           "-t", "60"]
    for o in opts:
        cmd.extend(["-o", o])

    if c.valgrind:
        # Valgrind wraps the postmaster; pg_ctl -w won't see readiness in time,
        # so callers must poll.  We approximate by launching via a wrapper.
        raise NotImplementedError("valgrind startup handled by the caller")

    res = _run(cmd)
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        sys.stderr.write(read_log_tail(c, 60))
        raise RuntimeError(f"pg_ctl start failed for {c.role}")
    wait_for_ready(c)


def wait_for_ready(c: Cluster, attempts: int = 60) -> None:
    last = None
    for _ in range(attempts):
        try:
            conn = psycopg.connect(**c.conn_params("postgres"), connect_timeout=3)
            conn.close()
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"{c.role} server never became ready: {last}")


def bootstrap_db(c: Cluster) -> None:
    """Create the test database and required extensions (idempotent)."""
    admin = psycopg.connect(**c.conn_params("postgres"), autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {DB_NAME}")
    finally:
        admin.close()
    conn = psycopg.connect(**c.conn_params(), autocommit=True)
    try:
        with conn.cursor() as cur:
            for ext in REQUIRED_EXTENSIONS:
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    finally:
        conn.close()


def bring_up(c: Cluster, fresh: bool) -> None:
    if fresh or not os.path.isdir(c.data_dir):
        initdb_fresh(c)
    start_cluster(c)
    bootstrap_db(c)


def read_log_tail(c: Cluster, nlines: int = 80) -> str:
    try:
        with open(c.logfile, "r", errors="replace") as f:
            return "".join(f.readlines()[-nlines:])
    except OSError:
        return "(no log)"


CRASH_MARKERS = ("TRAP:", "PANIC:", "server closed the connection",
                 "terminating connection because", "was terminated by signal",
                 "Failed Assert", "assertion")


def log_shows_crash(c: Cluster, since_size: int = 0) -> Optional[str]:
    """Return matching log lines if the server log shows a crash/assert."""
    try:
        with open(c.logfile, "r", errors="replace") as f:
            f.seek(since_size)
            text = f.read()
    except OSError:
        return None
    hits = [ln for ln in text.splitlines()
            if any(m in ln for m in CRASH_MARKERS)]
    return "\n".join(hits) if hits else None


def log_size(c: Cluster) -> int:
    try:
        return os.path.getsize(c.logfile)
    except OSError:
        return 0


# --------------------------------------------------------------------------
# Opclass catalog
# --------------------------------------------------------------------------
#
# Each OpClass knows how to:
#   - declare its column type and index opclass,
#   - emit a deterministic SQL expression for a column value given the series
#     value `g` (so both clusters produce identical data; ties controlled by
#     the `grid` flag),
#   - build a random WHERE predicate (a SQL literal baked in -> identical text
#     on both clusters),
#   - build a random ORDER BY distance expression (if it supports kNN), tagged
#     with whether that distance is a lossy lower bound (recheck).
#
# `rng` is a Python random.Random used ONLY for query constants, never for the
# bulk data (the data uses SQL random() under a fixed setseed).

OrderBy = Tuple[str, bool]   # (sql_expr, is_lower_bound)


@dataclass
class OpClass:
    name: str
    col_type: str
    extension: Optional[str]
    opclass: Optional[str]                       # index opclass, None = default
    value_sql: Callable[[bool], str]             # (grid) -> SQL value expr of g
    where_builders: List[Callable]               # (rng, grid) -> sql bool expr
    orderby_builders: List[Callable]             # (rng, grid) -> OrderBy
    can_ios: bool = False
    has_lower_bound: bool = False                # any orderby is lower-bound
    notes: str = ""

    @property
    def supports_ordered(self) -> bool:
        return bool(self.orderby_builders)


# ---- small deterministic literal helpers (Python side, for query constants) --

def _i(rng, lo=0, hi=1000):
    return rng.randint(lo, hi)


def _f(rng, lo=0.0, hi=1000.0):
    return round(rng.uniform(lo, hi), 3)


def _pt(rng):
    return f"point({_f(rng)},{_f(rng)})"


def _box(rng, maxspan=400):
    x = _f(rng); y = _f(rng)
    w = _f(rng, 1, maxspan); h = _f(rng, 1, maxspan)
    return f"box(point({x},{y}),point({round(x+w,3)},{round(y+h,3)}))"


# ---- value generators (SQL side, deterministic under setseed) ----------------
# grid=True snaps to a coarse lattice -> many exact-distance ties (probes the
# tie comparator).  grid=False is continuous -> ties essentially never.

def _v_point(grid):
    if grid:
        return "point((floor(random()*20)*50)::float8,(floor(random()*20)*50)::float8)"
    return "point(random()*1000, random()*1000)"


def _v_box(grid):
    if grid:
        return ("box(point((floor(random()*20)*50)::float8,(floor(random()*20)*50)::float8),"
                "point((floor(random()*20)*50+25)::float8,(floor(random()*20)*50+25)::float8))")
    return ("box(point(random()*1000, random()*1000),"
            "point(random()*1000+random()*200, random()*1000+random()*200))")


def _v_polygon(grid):
    # A polygon derived from a random box -> 4 vertices, MBR == the box.
    return f"polygon({_v_box(grid)})"


def _v_circle(grid):
    if grid:
        return "circle(point((floor(random()*20)*50)::float8,(floor(random()*20)*50)::float8), (floor(random()*5)*20+10)::float8)"
    return "circle(point(random()*1000, random()*1000), random()*80+5)"


def _v_int4range(grid):
    # Derive both bounds from g so lower <= upper always holds (two independent
    # random() draws could invert them, which int4range rejects).
    lo = "((g*7) % 1000)::int"
    return f"int4range({lo}, {lo} + ((g*13) % 100)::int + 1)"


def _v_inet(grid):
    return ("(('10.' || floor(random()*256)::int || '.' || floor(random()*256)::int "
            "|| '.' || floor(random()*256)::int)::inet)")


_TSV_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
              "golf", "hotel", "india", "juliet", "kilo", "lima"]


def _v_tsvector(grid):
    # 3 words chosen from a small vocab -> overlap, so @@ matches.
    picks = " || ' ' || ".join(
        f"(ARRAY{_TSV_WORDS})[floor(random()*{len(_TSV_WORDS)})::int + 1]"
        for _ in range(3)
    )
    return f"to_tsvector('simple', {picks})"


def _v_scalar_int(grid):
    return "(floor(random()*1000))::int4" if not grid else "(floor(random()*20)*50)::int4"


def _v_scalar_bigint(grid):
    return "(floor(random()*1000000))::int8" if not grid else "(floor(random()*20)*50000)::int8"


def _v_scalar_float8(grid):
    return "(random()*1000)::float8" if not grid else "(floor(random()*20)*50)::float8"


def _v_scalar_numeric(grid):
    return "round((random()*1000)::numeric, 2)" if not grid else "(floor(random()*20)*50)::numeric"


def _v_date(grid):
    return "(DATE '2000-01-01' + (floor(random()*3650))::int)"


def _v_timestamptz(grid):
    return "(TIMESTAMPTZ '2000-01-01' + (floor(random()*3650))::int * INTERVAL '1 day' + (random()*86400)::int * INTERVAL '1 second')"


def _v_cube(grid):
    if grid:
        return "cube(ARRAY[(floor(random()*10)*10)::float8,(floor(random()*10)*10)::float8,(floor(random()*10)*10)::float8])"
    return "cube(ARRAY[random()*100, random()*100, random()*100])"


_TRGM_SYL = ["ba", "be", "bo", "ka", "ki", "lo", "mu", "na", "ri", "se",
             "ta", "to", "vi", "za", "qu", "xa"]


def _v_text_trgm(grid):
    n = 3 if grid else 4
    picks = " || ".join(
        f"(ARRAY{_TRGM_SYL})[floor(random()*{len(_TRGM_SYL)})::int + 1]"
        for _ in range(n)
    )
    return picks


def _v_seg(grid):
    # Derive lo/hi from g so lo <= hi always holds (independent random draws
    # could swap the boundaries, which seg rejects).
    return ("(((g*7) % 100)::text || ' .. ' || "
            "(((g*7) % 100) + ((g*13) % 40) + 1)::text)::seg")


# ---- WHERE builders (Python side; literals baked in) -------------------------

def _w_point(rng, grid):
    choice = rng.choice(["box", "box", "left", "right", "above", "below"])
    if choice == "box":
        return f"c <@ {_box(rng)}"
    op = {"left": "<<", "right": ">>", "above": "|>>", "below": "<<|"}[choice]
    return f"c {op} {_pt(rng)}"


def _w_box(rng, grid):
    op = rng.choice(["&&", "&&", "@>", "<@", "~="])
    if op == "@>":
        return f"c @> {_pt(rng)}"
    return f"c {op} {_box(rng)}"


def _w_polygon(rng, grid):
    # Same-type bounding-box strategies (all indexable by poly_ops on the MBR).
    op = rng.choice(["&&", "&&", "<@", "@>", "~="])
    return f"c {op} {_v_polygon_literal(rng)}"


def _v_polygon_literal(rng):
    return f"polygon({_box(rng)})"


def _w_circle(rng, grid):
    # Same-type bounding-box strategies (indexable by circle_ops on the MBR).
    op = rng.choice(["&&", "&&", "<@", "@>"])
    return f"c {op} {_circle_literal(rng)}"


def _circle_literal(rng):
    return f"circle({_pt(rng)}, {_f(rng, 5, 200)})"


def _w_int4range(rng, grid):
    op = rng.choice(["&&", "@>", "<@", "<<", ">>", "-|-"])
    if op == "@>":
        return f"c @> {_i(rng)}"
    lo = _i(rng)
    return f"c {op} int4range({lo}, {lo + _i(rng, 1, 200)})"


def _w_inet(rng, grid):
    op = rng.choice(["&&", "<<=", ">>=", "&&"])
    prefix = rng.choice(["10.0.0.0/8", "10." + str(_i(rng, 0, 255)) + ".0.0/16",
                         "10.%d.%d.0/24" % (_i(rng, 0, 255), _i(rng, 0, 255))])
    return f"c {op} inet '{prefix}'"


def _w_tsvector(rng, grid):
    words = rng.sample(_TSV_WORDS, rng.randint(1, 2))
    q = (" | " if rng.random() < 0.5 else " & ").join(words)
    return f"c @@ to_tsquery('simple', '{q}')"


def _scalar_where(literal_fn):
    def build(rng, grid):
        op = rng.choice(["between", "between", "lt", "gt", "eq"])
        if op == "between":
            a = literal_fn(rng); b = literal_fn(rng)
            return f"c BETWEEN least({a},{b}) AND greatest({a},{b})"
        if op == "lt":
            return f"c < {literal_fn(rng)}"
        if op == "gt":
            return f"c > {literal_fn(rng)}"
        return f"c = {literal_fn(rng)}"
    return build


def _lit_int(rng):
    return str(_i(rng))


def _lit_bigint(rng):
    # Cast to int8 so the operator is int8<->int8 (gist_int8_ops has no
    # cross-type int8<->int4 operators, so an int4 literal defeats the index).
    return f"({_i(rng, 0, 1000000)})::int8"


def _lit_float8(rng):
    return f"({_f(rng)})::float8"


def _lit_numeric(rng):
    return f"({_f(rng)})::numeric"


def _lit_date(rng):
    return f"(DATE '2000-01-01' + {_i(rng, 0, 3650)})"


def _lit_timestamptz(rng):
    return f"(TIMESTAMPTZ '2000-01-01' + {_i(rng, 0, 3650)} * INTERVAL '1 day')"


def _w_cube(rng, grid):
    op = rng.choice(["&&", "@>", "<@"])
    return f"c {op} {_cube_literal(rng)}"


def _cube_literal(rng):
    a = [_f(rng, 0, 100) for _ in range(3)]
    b = [round(v + _f(rng, 1, 40), 3) for v in a]
    return f"cube(ARRAY{a}, ARRAY{b})"


def _w_trgm(rng, grid):
    syls = [rng.choice(_TRGM_SYL) for _ in range(rng.randint(2, 3))]
    s = "".join(syls)
    style = rng.choice(["sim", "sim", "like", "like_pre", "like_suf"])
    if style == "sim":
        return f"c % '{s}'::text"               # trigram similarity (index AND)
    if style == "like":
        return f"c LIKE '%{s}%'"
    if style == "like_pre":
        return f"c LIKE '{s}%'"
    return f"c LIKE '%{s}'"


def _w_seg(rng, grid):
    op = rng.choice(["&&", "<@", "@>", "<<", ">>"])
    lo = _i(rng, 0, 100); hi = lo + _i(rng, 1, 50)
    return f"c {op} '{lo} .. {hi}'::seg"


# ---- ORDER BY builders -------------------------------------------------------

def _ob_point(rng, grid):
    return (f"c <-> {_pt(rng)}", False)


def _ob_box(rng, grid):
    return (f"c <-> {_pt(rng)}", False)


def _ob_polygon(rng, grid):
    return (f"c <-> {_pt(rng)}", True)        # lower bound (MBR), recheck


def _ob_circle(rng, grid):
    return (f"c <-> {_pt(rng)}", True)        # lower bound (MBR), recheck


def _ob_scalar(literal_fn):
    def build(rng, grid):
        return (f"c <-> {literal_fn(rng)}", False)
    return build


def _ob_cube(rng, grid):
    op = rng.choice(["<->", "<#>", "<=>"])
    return (f"c {op} {_cube_literal(rng)}", False)


def _ob_trgm(rng, grid):
    if rng.random() < 0.5:
        syls = "".join(rng.choice(_TRGM_SYL) for _ in range(rng.randint(2, 3)))
        return (f"c <-> '{syls}'", False)     # plain trigram distance: exact
    syls = "".join(rng.choice(_TRGM_SYL) for _ in range(rng.randint(2, 3)))
    return (f"c <->> '{syls}'", True)         # word similarity: lower bound


def build_catalog() -> List[OpClass]:
    cat: List[OpClass] = [
        OpClass("point_ops", "point", None, None, _v_point,
                [_w_point], [_ob_point], can_ios=True),
        OpClass("box_ops", "box", None, None, _v_box,
                [_w_box], [_ob_box], can_ios=True),
        OpClass("poly_ops", "polygon", None, None, _v_polygon,
                [_w_polygon], [_ob_polygon], can_ios=False,
                has_lower_bound=True, notes="kNN distance is MBR lower bound"),
        OpClass("circle_ops", "circle", None, None, _v_circle,
                [_w_circle], [_ob_circle], can_ios=False,
                has_lower_bound=True, notes="kNN distance is MBR lower bound"),
        OpClass("range_ops", "int4range", None, None, _v_int4range,
                [_w_int4range], [], can_ios=False),
        OpClass("inet_ops", "inet", None, "inet_ops", _v_inet,
                [_w_inet], [], can_ios=False),
        OpClass("tsvector_ops", "tsvector", None, None, _v_tsvector,
                [_w_tsvector], [], can_ios=False),
        OpClass("bg_int4", "int4", "btree_gist", None, _v_scalar_int,
                [_scalar_where(_lit_int)], [_ob_scalar(_lit_int)], can_ios=True),
        OpClass("bg_int8", "int8", "btree_gist", None, _v_scalar_bigint,
                [_scalar_where(_lit_bigint)], [_ob_scalar(_lit_bigint)], can_ios=True),
        OpClass("bg_float8", "float8", "btree_gist", None, _v_scalar_float8,
                [_scalar_where(_lit_float8)], [_ob_scalar(_lit_float8)], can_ios=True),
        OpClass("bg_numeric", "numeric", "btree_gist", None, _v_scalar_numeric,
                [_scalar_where(_lit_numeric)], [], can_ios=True,
                notes="numeric gist has no <-> distance"),
        OpClass("bg_date", "date", "btree_gist", None, _v_date,
                [_scalar_where(_lit_date)], [_ob_scalar(_lit_date)], can_ios=True),
        OpClass("bg_timestamptz", "timestamptz", "btree_gist", None, _v_timestamptz,
                [_scalar_where(_lit_timestamptz)], [], can_ios=True,
                notes="timestamptz gist distance via interval; kept WHERE-only"),
        OpClass("cube_ops", "cube", "cube", None, _v_cube,
                [_w_cube], [_ob_cube], can_ios=True),
        OpClass("gist_trgm", "text", "pg_trgm", "gist_trgm_ops", _v_text_trgm,
                [_w_trgm], [_ob_trgm], can_ios=False,
                has_lower_bound=True, notes="<->> word similarity is lower bound"),
        OpClass("seg_ops", "seg", "seg", None, _v_seg,
                [_w_seg], [], can_ios=True),
    ]
    return cat


# --------------------------------------------------------------------------
# Table loading and query building (shared by both tools)
# --------------------------------------------------------------------------

def table_name(oc: "OpClass") -> str:
    return f"t_{oc.name}"


def index_name(oc: "OpClass") -> str:
    return f"t_{oc.name}_gix"


def seed_to_float(seed: int) -> float:
    """Map an arbitrary int to a setseed() argument in [-1, 1]."""
    return ((seed % 1999999) - 999999) / 1000000.0


def evict_relation(cur, relname: str) -> None:
    """Evict a relation's buffers (all forks: main, fsm, vm) from shared buffers
    so a following index scan's heap (and visibility-map) reads go through the
    prefetch read stream instead of hitting cache.  Best-effort."""
    try:
        cur.execute("SELECT pg_buffercache_evict_relation(%s::regclass)",
                    (relname,))
        cur.fetchall()
    except psycopg.Error:
        pass


def load_table(conn, oc: "OpClass", nrows: int, fillfactor: int,
               buffering: str, nullfrac: float, grid: bool, seed: int) -> None:
    """(Re)build oc's table+index identically given a fixed seed.

    Uses SQL random() under a fixed setseed so two clusters running this exact
    SQL produce byte-identical logical+physical data (same id->value mapping and
    insertion order), which is what makes ordered-scan tie behavior comparable.
    The connection must be autocommit (VACUUM).
    """
    tbl = table_name(oc)
    val = oc.value_sql(grid)
    if nullfrac > 0:
        valexpr = f"CASE WHEN random() < {nullfrac} THEN NULL ELSE {val} END"
    else:
        valexpr = val
    incl = " INCLUDE (id)" if oc.can_ios else ""
    opcl = f" {oc.opclass}" if oc.opclass else ""
    with conn.cursor() as cur:
        cur.execute("SET synchronize_seqscans = off")
        cur.execute(f"DROP TABLE IF EXISTS {tbl}")
        cur.execute(f"CREATE TABLE {tbl} (id bigint, c {oc.col_type}) "
                    f"WITH (fillfactor={fillfactor})")
        cur.execute(f"SELECT setseed({seed_to_float(seed)})")
        cur.execute(f"INSERT INTO {tbl} SELECT g, {valexpr} "
                    f"FROM generate_series(1, {nrows}) g")
        cur.execute(f"CREATE INDEX {index_name(oc)} ON {tbl} "
                    f"USING gist (c{opcl}){incl} WITH (buffering={buffering})")
        cur.execute(f"VACUUM (FREEZE, ANALYZE) {tbl}")


@dataclass
class GenQuery:
    sql: str                # patch query (may carry LIMIT)
    oracle_sql: str         # oracle ground truth -- ALWAYS unlimited
    intent: str             # ORDERED / UNORDERED / IOS / BITMAP
    compare: str            # "set" / "ios" / "ordered"
    limit: Optional[int]
    is_lower_bound: bool = False
    descr: str = ""


def build_query(oc: "OpClass", intent: str, rng, grid: bool,
                limit: Optional[int]) -> GenQuery:
    """Construct a SELECT that exercises `intent` on oc's table.

    Identity column is always `id` (= generate_series value, identical across
    clusters).  Ordered queries also project the exact distance as `dist`.

    LIMIT without ORDER BY returns an arbitrary subset that legitimately differs
    by access method, so the oracle is ALWAYS built unlimited; the comparator
    treats a LIMITed patch result as a subset of the full oracle answer.
    """
    tbl = table_name(oc)
    where = rng.choice(oc.where_builders)(rng, grid)
    lim = f" LIMIT {limit}" if limit is not None else ""

    if intent == ORDERED:
        expr, lb = rng.choice(oc.orderby_builders)(rng, grid)
        # Exclude NULL-distance rows so the comparison is over finite, fully
        # ordered distances; optionally add a real qual (mixes qual recheck with
        # distance recheck in one scan).
        wparts = ["c IS NOT NULL"]
        if rng.random() < 0.4:
            wparts.append(where)
        wclause = " WHERE " + " AND ".join(wparts)
        base = (f"SELECT ({expr})::float8 AS dist, id FROM {tbl}{wclause} "
                f"ORDER BY {expr}")
        return GenQuery(base + lim, base, ORDERED, "ordered", limit, lb, expr)

    if intent == IOS:
        # id comes from the INCLUDE column, c from the index key -> both are
        # reconstructed by gistgettransform.
        base = f"SELECT id, c::text FROM {tbl} WHERE {where}"
        return GenQuery(base + lim, base, IOS, "ios", limit, False, where)

    # UNORDERED or BITMAP
    base = f"SELECT id FROM {tbl} WHERE {where}"
    return GenQuery(base + lim, base, intent, "set", limit, False, where)


def run_rows(cur, sql: str, compare: str):
    """Execute `sql` and return rows shaped for the matching comparator."""
    cur.execute(sql)
    rows = cur.fetchall()
    if compare == "ordered":
        return [(r[0], r[1]) for r in rows]      # (dist, id)
    if compare == "ios":
        return [(r[0], r[1]) for r in rows]      # (id, value)
    return [(r[0],) for r in rows]               # (id,)


def compare_rows(gq: GenQuery, patch_rows, oracle_full_rows) -> CmpResult:
    if gq.compare == "ordered":
        return cmp_ordered(patch_rows, oracle_full_rows, gq.limit)
    if gq.compare == "ios":
        return cmp_ios(patch_rows, oracle_full_rows, gq.limit)
    return cmp_set(patch_rows, oracle_full_rows, gq.limit)


# --------------------------------------------------------------------------
# EXPLAIN gating
# --------------------------------------------------------------------------

# scan-type intents
ORDERED = "ordered"          # plain index scan with ORDER BY col <-> const
UNORDERED = "unordered"      # plain index scan, WHERE only
IOS = "ios"                  # index-only scan, WHERE only
BITMAP = "bitmap"            # bitmap heap scan

_FORCE_GUCS = {
    UNORDERED: {"enable_seqscan": "off", "enable_bitmapscan": "off",
                "enable_indexscan": "on", "enable_indexonlyscan": "off"},
    ORDERED:   {"enable_seqscan": "off", "enable_bitmapscan": "off",
                "enable_indexscan": "on", "enable_sort": "off"},
    IOS:       {"enable_seqscan": "off", "enable_bitmapscan": "off",
                "enable_indexscan": "on", "enable_indexonlyscan": "on"},
    BITMAP:    {"enable_seqscan": "off", "enable_indexscan": "off",
                "enable_indexonlyscan": "off", "enable_bitmapscan": "on"},
    "seqscan": {"enable_seqscan": "on", "enable_indexscan": "off",
                "enable_indexonlyscan": "off", "enable_bitmapscan": "off"},
}

_EXPECTED_NODE = {
    UNORDERED: "Index Scan",
    ORDERED: "Index Scan",
    IOS: "Index Only Scan",
    BITMAP: "Bitmap Heap Scan",
}


def force_scan_gucs(cur, intent: str) -> None:
    cur.execute("SET max_parallel_workers_per_gather = 0")
    for guc, val in _FORCE_GUCS[intent].items():
        cur.execute(f"SET {guc} = {val}")


def reset_scan_gucs(cur) -> None:
    for guc in ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan",
                "enable_bitmapscan", "enable_sort"):
        cur.execute(f"RESET {guc}")


def _walk_plans(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plans(child)


def explain_gate(cur, sql: str, intent: str, index_name: str) -> Tuple[bool, str]:
    """Confirm the plan uses the intended scan type on `index_name`.

    Returns (ok, reason).  Caller must have set the forcing GUCs already.
    """
    try:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql)
        plan = cur.fetchone()[0][0]["Plan"]
    except psycopg.Error as e:
        return False, f"explain_error: {e}".replace("\n", " ")[:200]

    nodes = list(_walk_plans(plan))
    want = _EXPECTED_NODE[intent]

    # No seq scan on our table is allowed for any index intent.
    for n in nodes:
        if n.get("Node Type") == "Seq Scan":
            return False, "seqscan_present"

    if intent == BITMAP:
        heap = [n for n in nodes if n.get("Node Type") == "Bitmap Heap Scan"]
        if not heap:
            return False, "no_bitmap_heap_scan"
        bidx = [n for n in nodes if n.get("Node Type") == "Bitmap Index Scan"
                and n.get("Index Name") == index_name]
        if not bidx:
            return False, "no_bitmap_index_scan_on_index"
        return True, "ok"

    scan = [n for n in nodes if n.get("Node Type") == want
            and n.get("Index Name") == index_name]
    if not scan:
        return False, f"no_{want.replace(' ', '_').lower()}_on_index"
    if intent == ORDERED:
        if not any("Order By" in n for n in scan):
            return False, "no_order_by_on_index_scan"
        if any(n.get("Node Type") == "Sort" for n in nodes):
            return False, "sort_node_present"
    return True, "ok"


# --------------------------------------------------------------------------
# Result comparison
# --------------------------------------------------------------------------

@dataclass
class CmpResult:
    status: str               # "MATCH", "BENIGN", "FAIL"
    kind: str = "ok"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("MATCH", "BENIGN")


_EPS_ABS = 1e-9
_EPS_REL = 1e-9


def _tol(x: float) -> float:
    return _EPS_ABS + _EPS_REL * max(1.0, abs(x))


def _as_dist(d):
    """Normalize a projected distance (None -> +inf, so NULLs sort last)."""
    return float("inf") if d is None else float(d)


def cmp_set(patch_rows, oracle_full_rows, limit) -> CmpResult:
    """Unordered / bitmap.  Rows are (id,) tuples.  `oracle_full_rows` is the
    UNLIMITED oracle answer.  Unlimited -> multiset equality; limited -> every
    patch row is a real match and the count is exactly min(limit, total)."""
    pc = Counter(patch_rows)
    oc = Counter(oracle_full_rows)
    if limit is None:
        if pc == oc:
            return CmpResult("MATCH")
        missing = list((oc - pc).elements())
        extra = list((pc - oc).elements())
        return CmpResult("FAIL", "SET_MISMATCH",
                         f"missing={missing[:8]} (n={len(missing)}) "
                         f"extra={extra[:8]} (n={len(extra)})")
    total = sum(oc.values())
    if len(patch_rows) != min(limit, total):
        return CmpResult("FAIL", "COUNT_MISMATCH",
                         f"patch={len(patch_rows)} expected={min(limit, total)} "
                         f"(limit={limit}, total={total})")
    bogus = list((pc - oc).elements())     # returned more often than truth has
    if bogus:
        return CmpResult("FAIL", "BOGUS_ROWS",
                         f"rows not in (full) oracle answer: {bogus[:8]}")
    return CmpResult("MATCH")


def cmp_ios(patch_rows, oracle_full_rows, limit) -> CmpResult:
    """Index-only scan.  Rows are (id, value).  Checks both membership and that
    the index-reconstructed value matches the heap truth."""
    pids = [r[0] for r in patch_rows]
    if len(set(pids)) != len(pids):
        return CmpResult("FAIL", "DUP_ID_IN_IOS", "duplicate id in IOS result")
    omap = dict(oracle_full_rows)
    total = len(oracle_full_rows)
    if limit is None:
        if len(patch_rows) != total:
            return cmp_set([(i,) for i in pids],
                           [(r[0],) for r in oracle_full_rows], None)
    else:
        if len(patch_rows) != min(limit, total):
            return CmpResult("FAIL", "COUNT_MISMATCH",
                             f"patch={len(patch_rows)} "
                             f"expected={min(limit, total)}")
    bogus = [i for i in pids if i not in omap]
    if bogus:
        return CmpResult("FAIL", "IOS_BOGUS_ID",
                         f"ids not in oracle answer: {bogus[:8]}")
    badv = [(i, v, omap[i]) for i, v in patch_rows if v != omap[i]]
    if badv:
        return CmpResult("FAIL", "IOS_VALUE_MISMATCH",
                         f"reconstructed != heap for {badv[:5]} (n={len(badv)})")
    return CmpResult("MATCH")


def cmp_ordered(patch_rows, oracle_full_rows, limit) -> CmpResult:
    """Ordered (kNN) comparison, robust to last-ULP distance noise.

    `patch_rows` = [(proj_dist, id)] in emit order.  `oracle_full_rows` =
    [(exact_dist, id)] -- the UNLIMITED master seqscan answer, used as the single
    source of distance truth (so cross-path float jitter never matters).

    Checks: (1) every returned id is a real match; (2) the patch emit order is
    non-decreasing in TRUE distance (within tolerance) -- the ordering contract;
    (3) the returned set is exactly the k smallest by distance, with boundary
    ties freely substitutable; (4) projected distances ~= true distances.
    Reordering within an equal-distance group is reported as benign.
    """
    omap = {i: _as_dist(d) for d, i in oracle_full_rows}
    patch_ids = [i for _, i in patch_rows]

    bogus = [i for i in patch_ids if i not in omap]
    if bogus:
        return CmpResult("FAIL", "ORDERED_BOGUS_ROW",
                         f"ids not in oracle answer: {bogus[:8]}")

    # (2) monotonic non-decreasing in true distance
    true_seq = [omap[i] for i in patch_ids]
    for k in range(1, len(true_seq)):
        if true_seq[k] < true_seq[k - 1] - _tol(true_seq[k - 1]):
            return CmpResult("FAIL", "ORDER_DIVERGENCE",
                             f"pos {k}: true_dist {true_seq[k]} < prior "
                             f"{true_seq[k-1]} (id {patch_ids[k]})")

    # (4) projected distance sanity
    wrongd = [(i, _as_dist(pd), omap[i]) for pd, i in patch_rows
              if abs(_as_dist(pd) - omap[i]) > _tol(omap[i])
              and not (omap[i] == float("inf") and _as_dist(pd) == float("inf"))]
    if wrongd:
        return CmpResult("FAIL", "WRONG_DISTANCE",
                         f"proj != true for {wrongd[:5]} (n={len(wrongd)})")

    # (3) completeness / smallest-k
    total = len(omap)
    expected_k = total if limit is None else min(limit, total)
    if len(patch_ids) != expected_k:
        return CmpResult("FAIL", "COUNT_MISMATCH",
                         f"patch={len(patch_ids)} expected={expected_k}")
    pset = set(patch_ids)
    dists_sorted = sorted(omap.values())
    if limit is None or total <= limit:
        if pset != set(omap):
            missing = list(set(omap) - pset)
            return CmpResult("FAIL", "INCOMPLETE",
                             f"missing closer rows: {missing[:8]}")
    else:
        threshold = dists_sorted[limit - 1]
        tt = _tol(threshold)
        over = [i for i in pset if omap[i] > threshold + tt]
        if over:
            return CmpResult("FAIL", "NOT_SMALLEST",
                             f"returned rows beyond k-th distance: {over[:8]}")
        must = [i for i in omap if omap[i] < threshold - tt]
        miss = [i for i in must if i not in pset]
        if miss:
            return CmpResult("FAIL", "MISSING_CLOSER",
                             f"omitted strictly-closer rows: {miss[:8]}")

    # benign within-tie reorder (only meaningful for the full, untruncated case)
    if limit is None or total <= limit:
        oracle_ids = [i for _, i in oracle_full_rows]
        if patch_ids != oracle_ids:
            return CmpResult("BENIGN", "WITHIN_EQUAL_DISTANCE_REORDER")
    return CmpResult("MATCH")


# --------------------------------------------------------------------------
# Repro dumping
# --------------------------------------------------------------------------

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gist_fuzz_results")


def ensure_results_dir() -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    return RESULTS_DIR


def dump_repro(tag: str, seed: int, iteration: int, body: str) -> str:
    ensure_results_dir()
    # Deterministic, sortable, no wall-clock dependency.
    fname = f"{tag}_seed{seed}_iter{iteration:06d}.txt"
    path = os.path.join(RESULTS_DIR, fname)
    with open(path, "w") as f:
        f.write(body)
    return path
