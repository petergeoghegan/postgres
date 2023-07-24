#!/usr/bin/env python3
"""Emit the report's markdown tables from results/ (NVMe) and results/delay/."""
import importlib.util
import json
import math
import os
import sys

W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"

MODELS = [
    ("legacy_rpc4", "stock (rpc=4)"),
    ("legacy_rpc1.1", "stock, rpc=1.1"),
    ("legacy_q10", "stock, spc=0.1 rpc=0.4"),
    ("m17_eicrel_bm", "A: penalty x 16/eic, both scans (the patch)"),
    ("m57_eicrel_bm_io", "A-whole: whole I/O x 16/eic (rejected)"),
    ("m14_little_bm", "B: penalty x 1/eic, both scans"),
    ("patch_B", "B + page floor (committed patch)"),
    ("m54_little_bm_io", "B-whole: whole I/O x 1/eic"),
    ("m4_little", "penalty x 1/eic, plain only"),
    ("m1_sqrt", "plain gets the bitmap's sqrt(P/T) discount"),
]
STATES = [("cold16", 16), ("cold100", 100), ("warm", 16), ("mix90_16", 16), ("mix90_100", 100)]


def load_profile(profile):
    os.environ["RIG_PROFILE"] = profile
    spec = importlib.util.spec_from_file_location("an" + profile, os.path.join(W, "analyze.py"))
    an = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(an)
    rows = an.load(os.path.join(an.RESULTS, "timing.jsonl"))
    t = an.timing_table(rows)
    costs = an.load(os.path.join(an.RESULTS, "costs.jsonl"))
    c = {}
    for r in costs:
        if r["kind"] == "single":
            cell = ("single", r["table"], r["layout"], r["rows"])
        elif r["kind"] == "two":
            cell = ("two", r["table"], "", (r["rows_a"], r["rows_b"]))
        else:
            cell = ("limit", r["table"], r["filtercol"], r["rows"])
        c[(cell, an.plan_label(r), r["model"], r["eic"])] = r
    return an, t, c


def regret(an, t, c, cell, ordered, model, s, eic):
    menu = an.menu_for(cell[0], ordered)
    seqcell = (cell[0], cell[1], "rand", cell[3]) if cell[0] == "single" else cell
    meas = {p: t.get((seqcell if p.startswith("seq") else cell, p)) for p in menu}
    est = {p: c.get((seqcell if p.startswith("seq") else cell, p, model, eic)) for p in menu}
    if any(v is None for v in meas.values()) or any(v is None for v in est.values()):
        return None
    times = {}
    for p in menu:
        if s.startswith("mix"):
            cold = "cold" + s.split("_")[1]
            if cold not in meas[p] or "warm" not in meas[p]:
                return None
            times[p] = 0.9 * meas[p]["warm"]["ms"] + 0.1 * meas[p][cold]["ms"]
        else:
            if s not in meas[p]:
                return None
            times[p] = meas[p][s]["ms"]
    choice = min(menu, key=lambda p: est[p]["total_cost"])
    best = min(times, key=times.get)
    return times[choice] / times[best]


def table(profile, kind, layouts=None):
    an, t, c = load_profile(profile)
    cells = sorted({k[0] for k in c if k[0][0] == kind}, key=lambda x: (x[1], x[2], str(x[3])))
    if layouts:
        cells = [x for x in cells if x[2] in layouts]
    base = {}
    out = []
    out.append("| model | " + " | ".join(f"{s}@{e}" for (s, e) in STATES) + " |")
    out.append("|---|" + "---|" * len(STATES))
    for (m, label) in MODELS:
        parts = []
        for (s, e) in STATES:
            v = []
            worse = better = 0
            for cell in cells:
                for o in ([False, True] if kind == "single" else [None]):
                    r = regret(an, t, c, cell, o, m, s, e)
                    if r is None:
                        continue
                    v.append(r)
                    b = base.get((cell, o, s, e))
                    if b is not None:
                        if r > b * 1.5:
                            worse += 1
                        elif r < b / 1.5:
                            better += 1
                    if m == "legacy_rpc4":
                        base[(cell, o, s, e)] = r
            if not v:
                parts.append("-")
                continue
            gm = math.exp(sum(math.log(x) for x in v) / len(v))
            n15 = sum(1 for x in v if x > 1.5)
            if m == "legacy_rpc4":
                parts.append(f"{gm:.2f} (max {max(v):.1f}, {n15}/{len(v)} >1.5x)")
            else:
                parts.append(f"{gm:.2f} (max {max(v):.1f}, {n15}/{len(v)}; +{better}/-{worse})")
        out.append(f"| {label} | " + " | ".join(parts) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    profile = sys.argv[1] if len(sys.argv) > 1 else "nvme"
    kind = sys.argv[2] if len(sys.argv) > 2 else "single"
    layouts = sys.argv[3].split(",") if len(sys.argv) > 3 else None
    print(table(profile, kind, layouts))
