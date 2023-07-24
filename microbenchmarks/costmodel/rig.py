#!/usr/bin/env python3
"""Cost model rig: plain index scan vs bitmap heap scan vs seq scan.

Measures actual execution time (EXPLAIN ANALYZE, TIMING OFF, BUFFERS, IO)
of every plan shape the planner could choose for a grid of range scans, with
the heap either fully cached in shared_buffers ("warm") or evicted from both
shared_buffers and the OS page cache ("cold"; the index and the VM stay
warm).  A separate phase records the planner's cost estimate for each plan
shape under each candidate cost model (debug_index_cost_model GUC of the
scratch build) so that the planner's choice can be scored against the
measured times.

Subcommands:
  run    timing grid            -> results/timing.jsonl
  costs  cost estimates         -> results/costs.jsonl
  state  print cache state of a table (debugging)
"""

import argparse
import ctypes
import json
import mmap
import os
import re
import sys
import time
from datetime import datetime

import psycopg

W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
# RIG_PROFILE=nvme (default): scratch cluster on NVMe, full-size tables.
# RIG_PROFILE=delay: the user's dm-delay cluster (1 ms read latency),
# half-size tables in its own "costmodel" database, results/delay/.
PROFILE = os.environ.get("RIG_PROFILE", "nvme")
if PROFILE == "delay":
    CONN = f"host={W}/sock port=54330 dbname=costmodel"
    RESULTS = f"{W}/results/delay"
    SCALE = 2
else:
    CONN = f"host={W}/sock port=54329 dbname=costmodel"
    RESULTS = f"{W}/results"
    SCALE = 1

# ---------------------------------------------------------------------------
# Grid definitions

TABLES = {
    "td": dict(n=10_000_000 // SCALE,
               rows=[r // SCALE for r in [1000, 10000, 50000, 100000, 200000,
                                          500000, 1000000, 2000000, 5000000]]),
    "ts": dict(n=200_000 // SCALE,
               rows=[r // SCALE for r in [20, 200, 1000, 2000, 4000, 10000,
                                          20000, 40000, 100000]]),
}
LAYOUTS = {"corr": "a_corr", "rand": "a_rand", "run4": "a_run4", "jit": "a_jit",
           # expression indexes added after the first pass (see extra_indexes.sql)
           "dup64": "hashint4((id - 1) / 64)",   # td only
           "run16": "hashint4((id - 1) / 16)"}   # ts only
HASH_LAYOUTS = {"dup64": "td", "run16": "ts"}
IDXNAME = {"corr": "corr", "rand": "rand", "run4": "run4", "jit": "jit",
           "brand": "brand", "clate": "clate", "dup64": "dup64", "run16": "run16"}

PLAN_GUCS = {
    "plain": {"enable_indexscan": "on", "enable_bitmapscan": "off",
              "enable_seqscan": "off"},
    "bitmap": {"enable_indexscan": "off", "enable_bitmapscan": "on",
               "enable_seqscan": "off"},
    "seq": {"enable_indexscan": "off", "enable_bitmapscan": "off",
            "enable_seqscan": "on"},
    "any": {"enable_indexscan": "on", "enable_bitmapscan": "on",
            "enable_seqscan": "on"},
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# OS page cache helpers (mincore / fadvise), after prefetch_benchmark.py

_libc = None


def libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL("libc.so.6", use_errno=True)
        _libc.mmap.restype = ctypes.c_void_p
        _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_long]
        _libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        _libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_void_p]
    return _libc


def resident_bytes(path):
    size = os.path.getsize(path)
    if size == 0:
        return 0
    fd = os.open(path, os.O_RDONLY)
    try:
        addr = libc().mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
    finally:
        os.close(fd)
    try:
        npages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
        vec = (ctypes.c_ubyte * npages)()
        if libc().mincore(ctypes.c_void_p(addr), size, vec) != 0:
            return None
        buf = bytes(memoryview(vec))
        return (len(buf) - buf.count(0)) * mmap.PAGESIZE
    finally:
        libc().munmap(ctypes.c_void_p(addr), size)


def fadvise_dontneed(files):
    for path in files:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


class Rig:
    def __init__(self):
        self.conn = psycopg.connect(CONN, autocommit=True)
        self.datadir = self.q1("SHOW data_directory")
        self._files = {}

    def q1(self, sql, *args):
        with self.conn.cursor() as cur:
            cur.execute(sql, args or None)
            row = cur.fetchone()
            return row[0] if row else None

    def exec(self, sql):
        with self.conn.cursor() as cur:
            cur.execute(sql)

    def set_gucs(self, gucs):
        for k, v in gucs.items():
            self.exec(f"SET {k} = '{v}'")

    def reset_gucs(self):
        self.exec("RESET ALL")

    # -- cache state -------------------------------------------------------

    def main_fork_files(self, rel):
        if rel in self._files and all(os.path.exists(f) for f in self._files[rel]):
            return self._files[rel]
        relpath = self.q1("SELECT pg_relation_filepath(%s)", rel)
        full = os.path.join(self.datadir, relpath)
        directory, node = os.path.split(full)
        pat = re.compile(r"^%s(\.\d+)?$" % re.escape(node))
        files = sorted(os.path.join(directory, e.name)
                       for e in os.scandir(directory) if pat.match(e.name))
        self._files[rel] = files
        return files

    def sb_blocks(self, rel):
        """Main-fork blocks of rel in shared_buffers."""
        return self.q1("""SELECT count(*) FROM pg_buffercache b, pg_class c
                          WHERE c.relname = %s
                            AND b.relfilenode = pg_relation_filenode(c.oid)
                            AND b.reldatabase = (SELECT oid FROM pg_database
                                                 WHERE datname = current_database())
                            AND b.relforknumber = 0""", rel)

    def evict(self, rel):
        """Evict rel (all forks) from shared_buffers and its main fork from
        the OS page cache; verify both."""
        for attempt in range(5):
            with self.conn.cursor() as cur:
                cur.execute("SELECT * FROM pg_buffercache_evict_relation(%s)", (rel,))
                evicted, flushed, skipped = cur.fetchone()
            if skipped == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"evict {rel}: {skipped} buffers skipped")
        files = self.main_fork_files(rel)
        fadvise_dontneed(files)
        res = sum(resident_bytes(f) or 0 for f in files)
        if res > 4 * 1024 * 1024:
            # retry once; readahead can race with the first drop
            time.sleep(0.2)
            fadvise_dontneed(files)
            res = sum(resident_bytes(f) or 0 for f in files)
        left = self.sb_blocks(rel)
        if left or res > 4 * 1024 * 1024:
            raise RuntimeError(f"evict {rel}: sb_blocks={left} os_resident={res}")

    def prewarm(self, rel, fork="main"):
        self.exec(f"SELECT pg_prewarm('{rel}', 'buffer', '{fork}')")

    def state(self, rel):
        files = self.main_fork_files(rel)
        res = sum(resident_bytes(f) or 0 for f in files)
        return dict(sb_blocks=self.sb_blocks(rel), os_resident_mb=res / 2**20)

    # -- explain -----------------------------------------------------------

    def explain_analyze(self, sql):
        with self.conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, TIMING OFF, BUFFERS, IO, FORMAT JSON) " + sql)
            return cur.fetchone()[0][0]

    def explain(self, sql):
        with self.conn.cursor() as cur:
            cur.execute("EXPLAIN (FORMAT JSON) " + sql)
            return cur.fetchone()[0][0]


# ---------------------------------------------------------------------------
# plan tree helpers

def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def plan_shape(plan):
    """Short description of a plan tree, e.g. 'Sort>BitmapHeap(td_rand)'."""
    parts = []
    for n in walk(plan):
        t = n["Node Type"]
        if t == "Index Scan":
            parts.append(f"Index({n['Index Name']})")
        elif t == "Index Only Scan":
            parts.append(f"IOS({n['Index Name']})")
        elif t == "Bitmap Index Scan":
            parts.append(f"BI({n['Index Name']})")
        elif t == "Bitmap Heap Scan":
            parts.append("BitmapHeap")
        elif t == "Seq Scan":
            parts.append("Seq")
        elif t in ("Sort", "Limit", "BitmapAnd", "BitmapOr", "Incremental Sort",
                   "Gather", "Gather Merge"):
            parts.append(t.replace(" ", ""))
    return ">".join(parts)


def scan_stats(plan):
    """Pull buffers / read stream stats off the heap-touching scan node."""
    out = {}
    for n in walk(plan):
        if n["Node Type"] in ("Index Scan", "Bitmap Heap Scan", "Seq Scan",
                              "Index Only Scan"):
            out["scan_hit"] = n.get("Shared Hit Blocks")
            out["scan_read"] = n.get("Shared Read Blocks")
            out["io_count"] = n.get("I/O Count")
            out["io_waits"] = n.get("I/O Waits")
            out["io_size"] = n.get("Average I/O Size")
            out["io_inprog"] = n.get("Average I/Os In Progress")
            out["pf_avg"] = n.get("Average Prefetch Distance")
            out["pf_max"] = n.get("Max Prefetch Distance")
            out["pf_cap"] = n.get("Prefetch Capacity")
            out["actual_rows"] = n.get("Actual Rows")
            if n["Node Type"] == "Bitmap Heap Scan":
                out["exact_pages"] = n.get("Exact Heap Blocks")
                out["lossy_pages"] = n.get("Lossy Heap Blocks")
            break
    for n in walk(plan):
        if n["Node Type"] == "Sort":
            out["sort_method"] = n.get("Sort Method")
            out["sort_space_kb"] = n.get("Sort Space Used")
            out["sort_space_type"] = n.get("Sort Space Type")
    return out


# ---------------------------------------------------------------------------
# cells

def range_sql(table, col, lo, hi, ordered):
    sql = f"SELECT * FROM {table} WHERE {col} BETWEEN {lo} AND {hi}"
    if ordered:
        sql += f" ORDER BY {col}"
    return sql


def single_index_cells(tables, layouts):
    """(table, layout, rows) -> list of (plan, ordered, sql, index)."""
    for t in tables:
        n = TABLES[t]["n"]
        for rows in TABLES[t]["rows"]:
            for lay in layouts:
                if lay in HASH_LAYOUTS:
                    if HASH_LAYOUTS[lay] != t:
                        continue
                    # hash values are uniform over int4: a range covering
                    # rows/n of the key space selects ~rows rows (whole
                    # groups of 64 or 16 rows)
                    lo = -2**31 + 2**30
                    hi = lo + int(rows / n * 2**32) - 1
                else:
                    lo = n // 4 + 1
                    hi = lo + rows - 1
                col = LAYOUTS[lay]
                idx = f"{t}_{IDXNAME[lay]}"
                menu = [("plain", False), ("bitmap", False), ("bitmap", True)]
                if lay == "rand":
                    menu += [("seq", False), ("seq", True)]
                for plan, ordered in menu:
                    yield dict(kind="single", table=t, layout=lay, rows=rows,
                               lo=lo, hi=hi, plan=plan, ordered=ordered,
                               index=idx, sql=range_sql(t, col, lo, hi, ordered))


# two-index cells: a_corr range (sequential, wide) AND b_rand range (random,
# narrow).  Forced plans use a "+ 0" on the other column to make it
# non-indexable; the unforced ("any") variant keeps both indexable.
TWO_A = {"td": [r // SCALE for r in [200000, 500000, 1000000, 2500000, 5000000]],
         "ts": [r // SCALE for r in [4000, 10000, 20000, 50000, 100000]]}
TWO_B = {"td": [r // SCALE for r in [10000, 50000, 100000, 200000, 500000]],
         "ts": [r // SCALE for r in [200, 1000, 2000, 4000, 10000]]}


def two_index_cells(tables):
    for t in tables:
        n = TABLES[t]["n"]
        for ra in TWO_A[t]:
            for rb in TWO_B[t]:
                alo = n // 4 + 1
                ahi = alo + ra - 1
                blo = n // 4 + 1
                bhi = blo + rb - 1
                qa = f"a_corr BETWEEN {alo} AND {ahi}"
                qb = f"b_rand BETWEEN {blo} AND {bhi}"
                qa0 = f"a_corr + 0 BETWEEN {alo} AND {ahi}"
                qb0 = f"b_rand + 0 BETWEEN {blo} AND {bhi}"
                base = f"SELECT * FROM {t} WHERE "
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="plain_a", ordered=False, index=f"{t}_corr",
                           sql=base + f"{qa} AND {qb0}",
                           gucs=PLAN_GUCS["plain"])
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="plain_b", ordered=False, index=f"{t}_brand",
                           sql=base + f"{qa0} AND {qb}",
                           gucs=PLAN_GUCS["plain"])
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="bitmap_a", ordered=False, index=f"{t}_corr",
                           sql=base + f"{qa} AND {qb0}",
                           gucs=PLAN_GUCS["bitmap"])
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="bitmap_b", ordered=False, index=f"{t}_brand",
                           sql=base + f"{qa0} AND {qb}",
                           gucs=PLAN_GUCS["bitmap"])
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="bitmap_any", ordered=False, index=None,
                           sql=base + f"{qa} AND {qb}",
                           gucs=PLAN_GUCS["bitmap"])
                yield dict(kind="two", table=t, rows_a=ra, rows_b=rb,
                           plan="seq", ordered=False, index=None,
                           sql=base + f"{qa} AND {qb}",
                           gucs=PLAN_GUCS["seq"])


# LIMIT cells: ORDER BY a_corr LIMIT 100 with a filter that is either
# uniformly spread (b_rand) or concentrated in the last 10% of the heap
# (c_late).
LIMIT_ROWS = {"td": [r // SCALE for r in [1000, 10000, 100000, 1000000]],
              "ts": [r // SCALE for r in [20, 200, 2000, 20000]]}


def limit_cells(tables):
    for t in tables:
        for col in ("b_rand", "c_late"):
            for rows in LIMIT_ROWS[t]:
                idx = f"{t}_brand" if col == "b_rand" else f"{t}_clate"
                base = f"SELECT * FROM {t} WHERE {col} <= {rows} ORDER BY a_corr LIMIT 100"
                yield dict(kind="limit", table=t, filtercol=col, rows=rows,
                           plan="plain_corr", ordered=True, index=f"{t}_corr",
                           sql=f"SELECT * FROM {t} WHERE {col} + 0 <= {rows} ORDER BY a_corr LIMIT 100",
                           gucs=PLAN_GUCS["plain"])
                yield dict(kind="limit", table=t, filtercol=col, rows=rows,
                           plan="plain_sel", ordered=True, index=idx,
                           sql=f"SELECT * FROM {t} WHERE {col} <= {rows} ORDER BY a_corr + 0 LIMIT 100",
                           gucs=PLAN_GUCS["plain"])
                yield dict(kind="limit", table=t, filtercol=col, rows=rows,
                           plan="bitmap_sel", ordered=True, index=idx,
                           sql=f"SELECT * FROM {t} WHERE {col} <= {rows} ORDER BY a_corr + 0 LIMIT 100",
                           gucs=PLAN_GUCS["bitmap"])
                yield dict(kind="limit", table=t, filtercol=col, rows=rows,
                           plan="any", ordered=True, index=None, sql=base,
                           gucs=PLAN_GUCS["any"])


# ---------------------------------------------------------------------------

def cell_key(c, cache, eic, run):
    k = {k: v for k, v in c.items() if k not in ("sql", "gucs")}
    k.update(cache=cache, eic=eic, run=run)
    return json.dumps(k, sort_keys=True)


def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                done.add(r["key"])
    return done


def prepare_cache(rig, c, cache):
    t = c["table"]
    if cache == "cold":
        rig.evict(t)
        rig.prewarm(t, "vm")
        for idx in index_list(c):
            rig.prewarm(idx)
    else:
        rig.prewarm(t)
        rig.prewarm(t, "vm")
        for idx in index_list(c):
            rig.prewarm(idx)


def index_list(c):
    t = c["table"]
    if c.get("index"):
        return [c["index"]]
    # unforced / bitmap_any: every index the planner might use
    if c["kind"] == "two":
        return [f"{t}_corr", f"{t}_brand"]
    if c["kind"] == "limit":
        return [f"{t}_corr", f"{t}_brand", f"{t}_clate"]
    return []


def run_timing(args):
    rig = Rig()
    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, args.out)
    done = load_done(out)
    tables = args.tables.split(",")
    layouts = args.layouts.split(",")
    cells = []
    if "single" in args.kinds:
        cells += list(single_index_cells(tables, layouts))
    if "two" in args.kinds:
        cells += list(two_index_cells(tables))
    if "limit" in args.kinds:
        cells += list(limit_cells(tables))
    caches = args.caches.split(",")
    eics = [int(e) for e in args.eics.split(",")]
    plan_total = 0
    for c in cells:
        for cache in caches:
            for eic in (eics if cache == "cold" else [eics[0]]):
                plan_total += 1
    log(f"{len(cells)} cells, {plan_total} (cell,cache,eic) combos, output {out}")
    n = 0
    with open(out, "a") as f:
        for c in cells:
            gucs = c.get("gucs") or PLAN_GUCS[c["plan"]]
            for cache in caches:
                for eic in (eics if cache == "cold" else [eics[0]]):
                    runs = args.runs_cold if cache == "cold" else args.runs_warm
                    for run in range(runs):
                        key = cell_key(c, cache, eic, run)
                        if key in done:
                            continue
                        rig.reset_gucs()
                        rig.set_gucs(gucs)
                        rig.set_gucs({"effective_io_concurrency": eic,
                                      "work_mem": "1GB",
                                      "random_page_cost": 4.0,
                                      "seq_page_cost": 1.0,
                                      "max_parallel_workers_per_gather": 0})
                        prepare_cache(rig, c, cache)
                        t0 = time.monotonic()
                        plan = rig.explain_analyze(c["sql"])
                        wall = time.monotonic() - t0
                        rec = {k: v for k, v in c.items() if k not in ("gucs",)}
                        rec.update(key=key, cache=cache, eic=eic, run=run,
                                   exec_ms=plan["Execution Time"],
                                   plan_ms=plan.get("Planning Time"),
                                   wall_ms=wall * 1000.0,
                                   shape=plan_shape(plan["Plan"]),
                                   total_cost=plan["Plan"]["Total Cost"],
                                   startup_cost=plan["Plan"]["Startup Cost"],
                                   plan_rows=plan["Plan"]["Plan Rows"],
                                   ts=datetime.now().isoformat())
                        rec.update(scan_stats(plan["Plan"]))
                        f.write(json.dumps(rec) + "\n")
                        f.flush()
                        n += 1
                        log(f"{c['kind']} {c['table']} {c.get('layout', c.get('filtercol', ''))} "
                            f"rows={c.get('rows', (c.get('rows_a'), c.get('rows_b')))} "
                            f"{c['plan']}{'+ord' if c['ordered'] else ''} {cache} eic={eic} "
                            f"run{run}: {rec['exec_ms']:.1f} ms  shape={rec['shape']} "
                            f"read={rec.get('scan_read')} io={rec.get('io_count')}/{rec.get('io_size')}")
    log(f"done, {n} new rows")


MODELS = {
    # name -> gucs
    "legacy_rpc4": {"debug_index_cost_model": 0, "random_page_cost": 4.0},
    "legacy_rpc2": {"debug_index_cost_model": 0, "random_page_cost": 2.0},
    "legacy_rpc1.1": {"debug_index_cost_model": 0, "random_page_cost": 1.1},
}


def run_costs(args):
    rig = Rig()
    out = os.path.join(RESULTS, args.out)
    tables = args.tables.split(",")
    layouts = args.layouts.split(",")
    cells = []
    if "single" in args.kinds:
        cells += list(single_index_cells(tables, layouts))
        # plus the unforced choice per (table, layout, rows, ordered)
        seen = set()
        for c in list(cells):
            k = (c["table"], c["layout"], c["rows"], c["ordered"])
            if c["kind"] == "single" and k not in seen:
                seen.add(k)
                cells.append(dict(c, plan="any", index=None))
    if "two" in args.kinds:
        cells += list(two_index_cells(tables))
        seen = set()
        for c in list(cells):
            if c["kind"] == "two":
                k = (c["table"], c["rows_a"], c["rows_b"])
                if k not in seen:
                    seen.add(k)
                    cells.append(dict(c, plan="any", index=None,
                                      sql=re.sub(r" \+ 0", "", c["sql"]),
                                      gucs=PLAN_GUCS["any"]))
    if "limit" in args.kinds:
        cells += list(limit_cells(tables))
    models = json.load(open(args.models)) if args.models else MODELS
    log(f"{len(cells)} cells x {len(models)} models -> {out}")
    with open(out, "a") as f:
        for c in cells:
            gucs = c.get("gucs") or PLAN_GUCS[c["plan"]]
            for mname, mgucs in models.items():
                rig.reset_gucs()
                rig.set_gucs(gucs)
                rig.set_gucs({"effective_io_concurrency": args.eic,
                              "work_mem": "1GB",
                              "random_page_cost": 4.0,
                              "seq_page_cost": 1.0,
                              "max_parallel_workers_per_gather": 0})
                rig.set_gucs(mgucs)
                plan = rig.explain(c["sql"])
                rec = {k: v for k, v in c.items() if k not in ("gucs",)}
                rec.update(model=mname, model_gucs=mgucs, eic=args.eic,
                           shape=plan_shape(plan["Plan"]),
                           total_cost=plan["Plan"]["Total Cost"],
                           startup_cost=plan["Plan"]["Startup Cost"],
                           plan_rows=plan["Plan"]["Plan Rows"])
                f.write(json.dumps(rec) + "\n")
    log("done")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tables", default="td,ts")
    r.add_argument("--layouts", default="corr,rand,run4,jit")
    r.add_argument("--kinds", default="single")
    r.add_argument("--caches", default="cold,warm")
    r.add_argument("--eics", default="16,100")
    r.add_argument("--runs-cold", type=int, default=2)
    r.add_argument("--runs-warm", type=int, default=3)
    r.add_argument("--out", default="timing.jsonl")
    c = sub.add_parser("costs")
    c.add_argument("--tables", default="td,ts")
    c.add_argument("--layouts", default="corr,rand,run4,jit")
    c.add_argument("--kinds", default="single")
    c.add_argument("--eic", type=int, default=16)
    c.add_argument("--models", default=None, help="json file name->gucs")
    c.add_argument("--out", default="costs.jsonl")
    s = sub.add_parser("state")
    s.add_argument("table")
    args = ap.parse_args()
    if args.cmd == "run":
        run_timing(args)
    elif args.cmd == "costs":
        run_costs(args)
    elif args.cmd == "state":
        print(Rig().state(args.table))


if __name__ == "__main__":
    main()
