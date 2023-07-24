#!/usr/bin/env python3
"""Add the 90%-cached columns and outputs to illustration.md (and the
tree copy's README.md): rewrite the two summary tables and append a section
with the eic=16 outputs."""
import json
import os
import re
import sys

W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
OUT = f"{W}/results/illustration"
s90 = json.load(open(f"{OUT}/summary_cached90.json"))
s = json.load(open(f"{OUT}/summary.json"))
sios = json.load(open(f"{OUT}/ios_summary.json"))


def rewrite(path, header_extra):
    text = open(path).read()
    # range-scan summary table
    old_hdr = "| query | plan | fully cached | heap uncached, eic=100 | heap uncached, eic=16 |"
    new_hdr = "| query | plan | fully cached | 90% cached, eic=16 | 90% cached, eic=100 | heap uncached, eic=16 | heap uncached, eic=100 |"
    assert old_hdr in text, path
    rows = []
    for tb in ("t_dups", "t_runs", "t_sparse"):
        for plan, label in (("plain", "Index Scan"), ("bitmap", "Sort over Bitmap Heap Scan")):
            rows.append(f"| {tb} | {label} | {s[f'{tb}_{plan}_cached']:.0f} | {s90[f'{tb}_{plan}_cached90_eic16']:.0f} | "
                        f"{s90[f'{tb}_{plan}_cached90_eic100']:.0f} | {s[f'{tb}_{plan}_uncached_eic16']:.0f} | {s[f'{tb}_{plan}_uncached_eic100']:.0f} |")
    start = text.index(old_hdr)
    end = text.index("\n\n", start)
    text = text[:start] + new_hdr + "\n|---|---|---|---|---|---|---|\n" + "\n".join(rows) + text[end:]
    # IOS table: add 90% columns for the 20% row pair only (the table is in that state)
    old_ios_hdr = "| all-visible pages | plan | fully cached | heap uncached, eic=100 | heap uncached, eic=16 | stock planner picks (eic 16 and 100) | patch picks (eic 16 and 100) |"
    assert old_ios_hdr in text, path
    new_ios_hdr = "| all-visible pages | plan | fully cached | 90% cached, eic=16 | 90% cached, eic=100 | heap uncached, eic=16 | heap uncached, eic=100 | stock planner picks (eic 16 and 100) | patch picks (eic 16 and 100) |"
    start = text.index(old_ios_hdr)
    end = text.index("\n\n", start)
    block = text[start:end].splitlines()[2:]
    new_rows = []
    for line in block:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        pct, plan, cached, unc100, unc16, stock, patch = cells
        key = "ios" if plan == "Index Only Scan" else "bitmap"
        if pct == "20%":
            c16 = f"{s90[f'ios_vm20_{key}_cached90_eic16']:.1f}"
            c100 = f"{s90[f'ios_vm20_{key}_cached90_eic100']:.1f}"
        else:
            c16 = c100 = "-"
        new_rows.append(f"| {pct} | {plan} | {cached} | {c16} | {c100} | {unc16} | {unc100} | {stock} | {patch} |")
    text = text[:start] + new_ios_hdr + "\n|---|---|---|---|---|---|---|---|---|\n" + "\n".join(new_rows) + text[end:]
    text = text.replace("Best of 3 (cached) or 2 (uncached) runs; EXPLAIN (ANALYZE, BUFFERS, IO).",
                        "\"90% cached\" = the table prewarmed into shared_buffers, then every heap page whose block "
                        "number hashes to 0 mod 10 evicted with pg_buffercache_evict() and the table's files dropped "
                        "from the OS page cache, so 10% of the pages are isolated single-page device reads.  "
                        "Best of 3 (cached) or 2 (uncached, 90% cached) runs; EXPLAIN (ANALYZE, BUFFERS, IO).")
    # appended section with the eic=16 outputs at 90% cached
    lines = ["\n## 90% of heap pages cached (effective_io_concurrency=16): outputs\n"]
    for name, sql in (("t_dups", "SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp"),
                      ("t_runs", "SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id"),
                      ("t_sparse", "SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id"),
                      ("ios_vm20", "SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 70000 ORDER BY id  -- 20% of pages all-visible")):
        lines.append(f"### {name}\n\n```sql\n{sql};\n```\n")
        for plan in (("ios", "bitmap") if name == "ios_vm20" else ("plain", "bitmap")):
            txt = open(f"{OUT}/{name}_{plan}_cached90_eic16.txt").read().split("\n", 2)[2].rstrip()
            title = {"plain": "Index Scan", "ios": "Index Only Scan", "bitmap": "Sort over Bitmap Heap Scan"}[plan]
            lines.append(f"#### {title}\n\n```\n{txt}\n```\n")
    if "## 90% of heap pages cached" not in text:
        text += "\n".join(lines)
    open(path, "w").write(text)
    print("updated", path)


rewrite(f"{W}/illustration.md", "")
if len(sys.argv) > 1 and sys.argv[1] == "--tree":
    rewrite("/mnt/nvme/postgresql/patch/source/microbenchmarks/costmodel/README.md", "")
