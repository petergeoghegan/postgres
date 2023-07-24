#!/usr/bin/env python3
"""Summaries of the cost model rig results.

  python3 analyze.py times   [--table td] : measured min times per cell
  python3 analyze.py pagecost            : cold per-page costs relative to seq scan
  python3 analyze.py regret              : planner choice vs best plan, per model
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
PROFILE = os.environ.get("RIG_PROFILE", "nvme")
RESULTS = f"{W}/results/delay" if PROFILE == "delay" else f"{W}/results"

PAGES = {"td": 204082, "ts": 200000} if PROFILE != "delay" else {"td": 102041, "ts": 100000}


def load(path):
    out = []
    with open(path) as f:
        for line in f:
            out.append(json.loads(line))
    return out


def plan_label(r):
    return r["plan"] + ("+ord" if r.get("ordered") else "")


def timing_table(rows):
    """(kind, table, layout/filtercol, rows-key, plan_label) -> {state: min ms, stats}"""
    t = defaultdict(dict)
    for r in rows:
        if r["kind"] == "single":
            cell = ("single", r["table"], r["layout"], r["rows"])
        elif r["kind"] == "two":
            cell = ("two", r["table"], "", (r["rows_a"], r["rows_b"]))
        else:
            cell = ("limit", r["table"], r["filtercol"], r["rows"])
        state = f"{r['cache']}{r['eic']}" if r["cache"] == "cold" else "warm"
        d = t[(cell, plan_label(r))]
        cur = d.get(state)
        if cur is None or r["exec_ms"] < cur["ms"]:
            d[state] = dict(ms=r["exec_ms"], read=r.get("scan_read"),
                            io=r.get("io_count"), iosz=r.get("io_size"),
                            shape=r["shape"], rows=r.get("actual_rows"),
                            pf=r.get("pf_avg"), waits=r.get("io_waits"))
    return t


def cmd_times(args):
    rows = load(os.path.join(RESULTS, args.timing))
    t = timing_table(rows)
    cells = sorted({c for (c, p) in t}, key=lambda c: (c[0], c[1], c[2], str(c[3])))
    states = ["cold16", "cold100", "warm"]
    last = None
    for cell in cells:
        if args.table and cell[1] != args.table:
            continue
        if args.kind and cell[0] != args.kind:
            continue
        if cell[:3] != last:
            print(f"\n== {cell[0]} {cell[1]} {cell[2]}")
            last = cell[:3]
        plans = sorted(p for (c, p) in t if c == cell)
        for p in plans:
            d = t[(cell, p)]
            parts = []
            for s in states:
                if s in d:
                    e = d[s]
                    extra = ""
                    if s != "warm" and e["read"]:
                        extra = f" rd={e['read']} io={e['io']} sz={e['iosz']}"
                    parts.append(f"{s}={e['ms']:9.1f}{extra}")
            print(f"  rows={str(cell[3]):>18} {p:12} " + "  ".join(parts))


def cmd_pagecost(args):
    """Cold per-page I/O cost of each plan relative to a seq scan's per-page cost
    (same table, same eic): (cold - warm) / pages_read  /  ((seq_cold - seq_warm) / T)."""
    rows = load(os.path.join(RESULTS, args.timing))
    t = timing_table(rows)
    for table in ("td", "ts"):
        T = PAGES[table]
        for eic in (16, 100):
            st = f"cold{eic}"
            seq = t.get((("single", table, "rand", TABLE_ROWS_FOR_SEQ(table)), "seq"))
            seqs = [t[(c, "seq")] for (c, p) in t
                    if c[0] == "single" and c[1] == table and p == "seq"]
            seq_pp = None
            vals = []
            for d in seqs:
                if st in d and "warm" in d:
                    vals.append((d[st]["ms"] - d["warm"]["ms"]) / T)
            if vals:
                seq_pp = sorted(vals)[len(vals) // 2]
            print(f"\n== {table} eic={eic}: seq scan cold-warm per page = "
                  f"{seq_pp * 1000:.2f} us" if seq_pp else f"\n== {table} eic={eic}: no seq data")
            if not seq_pp:
                continue
            print(f"  {'layout':6} {'rows':>8} {'plan':8} {'pages':>7} {'dens%':>6} "
                  f"{'us/pg':>7} {'x seq':>6} {'io/pg':>6} {'sz':>5} {'pf':>6} {'waits':>6}")
            for (cell, p), d in sorted(t.items(), key=lambda kv: (kv[0][0][2], kv[0][0][3], kv[0][1])):
                if cell[0] != "single" or cell[1] != table or p not in ("plain", "bitmap", "seq"):
                    continue
                if st not in d or "warm" not in d:
                    continue
                e = d[st]
                pages = e["read"] or 0
                if pages < 1:
                    continue
                pp = (e["ms"] - d["warm"]["ms"]) / pages
                print(f"  {cell[2]:6} {cell[3]:>8} {p:8} {pages:>7} {100.0 * pages / T:6.1f} "
                      f"{pp * 1000:7.2f} {pp / seq_pp:6.2f} "
                      f"{(e['io'] or 0) / pages:6.2f} {e['iosz'] or 0:5.2f} {e['pf'] or 0:6.1f} {e['waits'] or 0:6}")


def TABLE_ROWS_FOR_SEQ(table):
    return None


# ---------------------------------------------------------------------------
# planner choice vs measured best

def menu_for(kind, ordered):
    if kind == "single":
        if ordered:
            return ["plain", "bitmap+ord", "seq+ord"]
        return ["plain", "bitmap", "seq"]
    if kind == "two":
        return ["plain_a", "plain_b", "bitmap_a", "bitmap_b", "bitmap_any", "seq"]
    if kind == "limit":
        return ["plain_corr+ord", "plain_sel+ord", "bitmap_sel+ord"]
    return []


def cmd_regret(args):
    rows = load(os.path.join(RESULTS, args.timing))
    t = timing_table(rows)
    costs = load(os.path.join(RESULTS, args.costs))
    # cost table: (cell, plan_label, model, eic) -> record
    c = {}
    for r in costs:
        if r["kind"] == "single":
            cell = ("single", r["table"], r["layout"], r["rows"])
        elif r["kind"] == "two":
            cell = ("two", r["table"], "", (r["rows_a"], r["rows_b"]))
        else:
            cell = ("limit", r["table"], r["filtercol"], r["rows"])
        c[(cell, plan_label(r), r["model"], r["eic"])] = r
    models = sorted({r["model"] for r in costs}, key=model_order)
    # (timing state, cost eic) pairs to score
    pairs = [("cold16", 16), ("cold100", 100), ("warm", 16), ("warm", 100),
             ("mix90_16", 16), ("mix90_100", 100), ("mix50_16", 16), ("mix50_100", 100)]
    if args.states:
        want = args.states.split(",")
        pairs = [p for p in pairs if p[0] in want]
    summary = {m: {p: [] for p in pairs} for m in models}
    detail = []
    cells = sorted({k[0] for k in c}, key=lambda c: (c[0], c[1], c[2], str(c[3])))
    for cell in cells:
        if args.kind and cell[0] != args.kind:
            continue
        if args.table and cell[1] != args.table:
            continue
        for ordered in ([False, True] if cell[0] == "single" else [None]):
            menu = menu_for(cell[0], ordered)
            seqcell = (cell[0], cell[1], "rand", cell[3]) if cell[0] == "single" else cell
            meas = {p: t.get((seqcell if p.startswith("seq") else cell, p)) for p in menu}
            if any(v is None for v in meas.values()):
                continue
            for m in models:
                for (s, eic) in pairs:
                    est = {p: c.get((seqcell if p.startswith("seq") else cell, p, m, eic))
                           for p in menu}
                    if any(v is None for v in est.values()):
                        continue
                    choice = min(menu, key=lambda p: est[p]["total_cost"])
                    times = {}
                    for p in menu:
                        if s.startswith("mix"):
                            frac = 0.9 if s.startswith("mix90") else 0.5
                            cold = "cold" + s.split("_")[1]
                            if cold in meas[p] and "warm" in meas[p]:
                                times[p] = frac * meas[p]["warm"]["ms"] + (1 - frac) * meas[p][cold]["ms"]
                        elif s in meas[p]:
                            times[p] = meas[p][s]["ms"]
                    if len(times) != len(menu):
                        continue
                    best = min(times, key=times.get)
                    regret = times[choice] / times[best]
                    summary[m][(s, eic)].append(regret)
                    detail.append((cell, ordered, m, s, eic, choice, best, regret,
                                   times[choice], times[best]))
    hdr = " ".join(f"{s + '@' + str(e):>24}" for (s, e) in pairs)
    print(f"{'model':18} {hdr}")
    print(f"{'':18} " + " ".join(f"{'geomean   max  n>1.5x':>24}" for p in pairs))
    for m in models:
        parts = []
        for p in pairs:
            v = summary[m][p]
            if not v:
                parts.append(f"{'-':>24}")
                continue
            gm = math.exp(sum(math.log(x) for x in v) / len(v))
            parts.append(f"{gm:7.3f} {max(v):6.1f} {sum(1 for x in v if x > 1.5):5d}/{len(v):<4d}")
        print(f"{m:18} " + " ".join(parts))
    # per-cell comparison against the stock model: how many cells does each
    # model make better / worse than legacy_rpc4 by more than 1.5x?
    base = "legacy_rpc4"
    if base in models:
        reg = {}
        for (cell, ordered, m, s, eic, choice, best, regret, tc, tb) in detail:
            reg[(cell, ordered, m, s, eic)] = regret
        print()
        print(f"{'vs stock':18} " + " ".join(f"{'better / worse (>1.5x)':>24}" for p in pairs))
        for m in models:
            if m == base:
                continue
            parts = []
            for (s, eic) in pairs:
                better = worse = n = 0
                for (cell, ordered, mm, ss, ee), r in reg.items():
                    if mm != m or ss != s or ee != eic:
                        continue
                    b = reg.get((cell, ordered, base, s, eic))
                    if b is None:
                        continue
                    n += 1
                    if r < b / 1.5:
                        better += 1
                    elif r > b * 1.5:
                        worse += 1
                parts.append(f"{better:10d} / {worse:<11d}")
            print(f"{m:18} " + " ".join(parts))
    if args.detail:
        print()
        for (cell, ordered, m, s, eic, choice, best, regret, tc, tb) in detail:
            if regret > args.detail and (not args.model or m == args.model) \
                    and (args.state is None or s == args.state):
                print(f"{m:14} {s:7}@{eic:<3} {cell[0]} {cell[1]} {cell[2]:5} rows={str(cell[3]):>18} "
                      f"ord={ordered!s:5} chose {choice:14} ({tc:8.1f} ms) best {best:14} "
                      f"({tb:8.1f} ms) regret {regret:5.2f}")


def model_order(m):
    order = ["legacy_rpc4", "legacy_rpc2", "legacy_rpc1.1"]
    return (order.index(m) if m in order else 10, m)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("times")
    a.add_argument("--timing", default="timing.jsonl")
    a.add_argument("--table", default=None)
    a.add_argument("--kind", default=None)
    b = sub.add_parser("pagecost")
    b.add_argument("--timing", default="timing.jsonl")
    r = sub.add_parser("regret")
    r.add_argument("--timing", default="timing.jsonl")
    r.add_argument("--costs", default="costs.jsonl")
    r.add_argument("--detail", type=float, default=0.0,
                   help="print cells whose regret exceeds this factor")
    r.add_argument("--model", default=None)
    r.add_argument("--state", default=None)
    r.add_argument("--states", default=None, help="comma list of states to score")
    r.add_argument("--kind", default=None)
    r.add_argument("--table", default=None)
    args = ap.parse_args()
    {"times": cmd_times, "pagecost": cmd_pagecost, "regret": cmd_regret}[args.cmd](args)


if __name__ == "__main__":
    main()
