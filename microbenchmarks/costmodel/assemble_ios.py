#!/usr/bin/env python3
"""Append the index-only-scan coverage series to illustration.md."""
import json, os, re
W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
OUT = f"{W}/results/illustration"
s = json.load(open(f"{OUT}/ios_summary.json"))
log = open(f"{W}/logs/illustrate_ios.log").read()
cov = {}
for m in re.finditer(r"== (vm\d+): relpages=(\d+) relallvisible=(\d+) \((\d+)%\)", log):
    cov[m.group(1)] = (int(m.group(2)), int(m.group(3)), m.group(4))
lines = []
lines.append("\n## Index-only scan with partial visibility-map coverage\n")
lines.append("Setup: `illustrate_setup_ios.sql` (t_sparse_ios: one primary and one filler row per page, "
             "random page order, unlogged).  Coverage is reduced by deleting the filler row on a "
             "pseudo-random set of pages and running `VACUUM (INDEX_CLEANUP off, ANALYZE)` + `REINDEX`, "
             "which leaves an LP_DEAD stub on those pages: no later VACUUM or scan can set them all-visible, "
             "so the index-only scan has to fetch them (the --ios suite's recipe).\n")
lines.append("```sql\nSELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 70000 ORDER BY id;\n```\n")
lines.append("| all-visible pages | plan | fully cached | heap uncached, eic=100 | heap uncached, eic=16 | stock planner picks | patch (eic=100) picks |")
lines.append("|---|---|---|---|---|---|---|")
for st in ("vm100", "vm50", "vm20"):
    choices = open(f"{OUT}/ios_{st}_choices.txt").read()
    def first(label):
        m = re.search(r"-- " + label + r"\n(.*)\n", choices)
        return re.sub(r"\s+\(cost=.*", "", m.group(1)).strip() if m else "?"
    stock = first("stock_eic100"); patch = first("patch_eic100")
    pct = cov.get(st, (0, 0, "?"))[2]
    for plan, label in (("ios", "Index Only Scan"), ("bitmap", "Sort over Bitmap Heap Scan")):
        lines.append(f"| {pct}% | {label} | {s[f'ios_{st}_{plan}_cached']:.1f} | {s[f'ios_{st}_{plan}_uncached_eic100']:.1f} | "
                     f"{s[f'ios_{st}_{plan}_uncached_eic16']:.1f} | {stock if plan == 'ios' else ''} | {patch if plan == 'ios' else ''} |")
lines.append("")
for st in ("vm100", "vm50", "vm20"):
    pct = cov.get(st, (0, 0, "?"))[2]
    lines.append(f"### {pct}% of pages all-visible: planner costs\n")
    lines.append("```\n" + open(f"{OUT}/ios_{st}_choices.txt").read().rstrip() + "\n```\n")
    for plan, state, title in (("ios", "cached", "Index Only Scan, fully cached"),
                               ("bitmap", "cached", "Bitmap heap scan + Sort, fully cached"),
                               ("ios", "uncached_eic100", "Index Only Scan, heap uncached, effective_io_concurrency=100"),
                               ("bitmap", "uncached_eic100", "Bitmap heap scan + Sort, heap uncached, effective_io_concurrency=100")):
        txt = open(f"{OUT}/ios_{st}_{plan}_{state}.txt").read().split("\n", 2)[2].rstrip()
        lines.append(f"#### {title}\n")
        lines.append("```\n" + txt + "\n```\n")
with open(f"{W}/illustration.md", "a") as f:
    f.write("\n".join(lines))
print("appended", len(lines), "lines")
