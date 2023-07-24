#!/usr/bin/env python3
"""Build microbenchmarks/costmodel/README.md (and illustration.md) from the
captured EXPLAIN outputs, with the 90%-cached case first everywhere."""
import json
import os
import re

W = "/mnt/nvme/postgresql/patch/claude_work/costmodel"
OUT = f"{W}/results/illustration"
s = json.load(open(f"{OUT}/summary.json"))
s90 = json.load(open(f"{OUT}/summary_cached90.json"))
sios = json.load(open(f"{OUT}/ios_summary.json"))
log = open(f"{W}/logs/illustrate_ios.log").read()
cov = {m.group(1): m.group(2) for m in re.finditer(r"== (vm\d+): relpages=\d+ relallvisible=\d+ \((\d+)%\)", log)}

QUERIES = [
    ("t_dups", "SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp",
     "clustered duplicates: 64 rows per grp on ~1.3 adjacent pages, groups in random order (the t_randomized shape); 1M rows, 32k of 182k heap pages"),
    ("t_runs", "SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id",
     "4-page runs in random order, unique key, correlation ~0; 1M rows, 20k of 182k heap pages"),
    ("t_sparse", "SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id",
     "one row per page, random order; 20k rows = 20k of 200k heap pages"),
]
IOS_SQL = "SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 130000"
PLAN_LABEL = {"plain": "Index Scan", "ios": "Index Only Scan", "bitmap": "Sort over Bitmap Heap Scan"}
IOS_PLAN_LABEL = {"ios": "Index Only Scan", "bitmap": "Bitmap Heap Scan"}
STATE_LABEL = {"cached90_eic16": "90% of heap pages cached, effective_io_concurrency=16",
               "cached90_eic100": "90% of heap pages cached, effective_io_concurrency=100",
               "cached": "fully cached",
               "uncached_eic16": "heap uncached, effective_io_concurrency=16",
               "uncached_eic100": "heap uncached, effective_io_concurrency=100"}
ORDER = ["cached90_eic16", "cached90_eic100", "cached", "uncached_eic16", "uncached_eic100"]


def body(path):
    return open(path).read().split("\n", 2)[2].rstrip()


def ms(name, plan, state):
    key = f"{name}_{plan}_{state}"
    if state.startswith("cached90"):
        return s90[key] if name != "ios_vm40" else s90[key]
    return (sios if name.startswith("ios_") else s)[key]


L = []
L.append("# Plain index scan vs bitmap heap scan + Sort, with index prefetching\n")
L.append("Setup: `illustrate_setup.sql` (three tables, 1.4-1.6 GB of heap each) and "
         "`illustrate_setup_ios.sql`.  Branch tip b83b554d2 (index-prefetch-post-v37.2) plus the cost "
         "model commits, release build, 16 GB shared_buffers, io_uring, io_combine_limit=16, stock "
         "planner settings (random_page_cost=4).  Local NVMe (Samsung 980 PRO).  EXPLAIN (ANALYZE, "
         "BUFFERS, IO), best of 2 or 3 runs.\n")
L.append("Cache states: \"90% cached\" = the table prewarmed into shared_buffers, then every heap page "
         "whose block number hashes to 0 mod 10 evicted with pg_buffercache_evict() and the table's "
         "files dropped from the OS page cache, so the scan finds 90% of its pages in shared_buffers "
         "and reads the other 10% as isolated single-page device reads: the uniform-miss model behind "
         "the cache-hit assumption of random_page_cost.  \"fully cached\" = everything in "
         "shared_buffers.  \"heap uncached\" = heap evicted from shared_buffers and from the OS page "
         "cache; index and visibility map warm in every state.\n")
L.append("## Summary (execution time, ms)\n")
L.append("| query | plan | 90% cached, eic=16 | 90% cached, eic=100 | fully cached | heap uncached, eic=16 | heap uncached, eic=100 |")
L.append("|---|---|---|---|---|---|---|")
for name, sql, desc in QUERIES:
    for plan in ("plain", "bitmap"):
        L.append(f"| {name} | {PLAN_LABEL[plan]} | " + " | ".join(f"{ms(name, plan, st):.0f}" for st in ORDER) + " |")
L.append("")
L.append("The stock planner picks the Sort over Bitmap Heap Scan for all three queries (costs in the "
         "outputs).  With the cost model change (random-access penalty of prefetched heap pages divided "
         "by effective_io_concurrency, both scan types) the planner picks the Index Scan for all three, "
         "at eic=16 and at eic=100 (`results/illustration/choices_patch.txt`).\n")
for name, sql, desc in QUERIES:
    L.append(f"## {name}: {desc}\n")
    L.append(f"```sql\n{sql};\n```\n")
    for st in ORDER:
        for plan in ("plain", "bitmap"):
            L.append(f"### {PLAN_LABEL[plan]}, {STATE_LABEL[st]}\n")
            L.append("```\n" + body(f"{OUT}/{name}_{plan}_{st}.txt") + "\n```\n")

L.append("## Index-only scan with partial visibility-map coverage\n")
L.append("Setup: `illustrate_setup_ios.sql` (t_sparse_ios: one primary and one filler row per page, "
         "random page order, unlogged).  Coverage is reduced by deleting the filler row on a "
         "pseudo-random set of pages and running `VACUUM (INDEX_CLEANUP off, ANALYZE)` + `REINDEX`, "
         "which leaves an LP_DEAD stub on those pages: no later VACUUM or scan can set them "
         "all-visible, so the index-only scan has to fetch them (the --ios suite's recipe).  The scan "
         "touches 40% of the pages; at 40% coverage the stock planner gives up on the index-only scan "
         "(costs in the outputs below), while the cost model change keeps it.\n")
L.append(f"```sql\n{IOS_SQL};\n```\n")
L.append("| all-visible pages | plan | 90% cached, eic=16 | 90% cached, eic=100 | fully cached | heap uncached, eic=16 | heap uncached, eic=100 | stock planner picks | with the cost change |")
L.append("|---|---|---|---|---|---|---|---|---|")
for st_cov in ("vm100", "vm60", "vm40"):
    pct = cov.get(st_cov, "?")
    for plan in ("ios", "bitmap"):
        c90_16 = f"{s90[f'ios_vm40_{plan}_cached90_eic16']:.1f}" if st_cov == "vm40" else "-"
        c90_100 = f"{s90[f'ios_vm40_{plan}_cached90_eic100']:.1f}" if st_cov == "vm40" else "-"
        row = (f"| {pct}% | {IOS_PLAN_LABEL[plan]} | {c90_16} | {c90_100} | {sios[f'ios_{st_cov}_{plan}_cached']:.1f} | "
               f"{sios[f'ios_{st_cov}_{plan}_uncached_eic16']:.1f} | {sios[f'ios_{st_cov}_{plan}_uncached_eic100']:.1f} | ")
        if plan == "ios":
            row += ("Bitmap Heap Scan" if st_cov == "vm40" else "Index Only Scan") + " | Index Only Scan |"
        else:
            row += " | |"
        L.append(row)
L.append("")
L.append("### 40% of pages all-visible\n")
L.append("Planner costs (stock at eic 16 and 100, then with the cost change at eic=100):\n")
L.append("```\n" + open(f"{OUT}/ios_vm40_choices.txt").read().rstrip() + "\n```\n")
for st in ORDER:
    for plan in ("ios", "bitmap"):
        if st.startswith("cached90"):
            path = f"{OUT}/ios_vm40_{plan}_{st}.txt"
        else:
            path = f"{OUT}/ios_vm40_{plan}_{st}.txt"
        L.append(f"#### {IOS_PLAN_LABEL[plan]}, {STATE_LABEL[st]}\n")
        L.append("```\n" + body(path) + "\n```\n")
for st_cov in ("vm100", "vm60"):
    pct = cov.get(st_cov, "?")
    L.append(f"### {pct}% of pages all-visible\n")
    L.append("```\n" + open(f"{OUT}/ios_{st_cov}_choices.txt").read().rstrip() + "\n```\n")
    for st in ("cached", "uncached_eic100"):
        for plan in ("ios", "bitmap"):
            L.append(f"#### {IOS_PLAN_LABEL[plan]}, {STATE_LABEL[st]}\n")
            L.append("```\n" + body(f"{OUT}/ios_{st_cov}_{plan}_{st}.txt") + "\n```\n")

text = "\n".join(L)
open(f"{W}/illustration.md", "w").write(text)
tree = "/mnt/nvme/postgresql/patch/source/microbenchmarks/costmodel/README.md"
hdr = ("Motivating examples and measurement rig for the index scan cost model change\n"
       "(see REPORT.md for the full study; raw timing/cost JSONL and the builds stay\n"
       "under claude_work/costmodel on this machine).  The scripts carry absolute\n"
       "paths for this machine, like the other microbenchmarks.\n\n")
open(tree, "w").write(hdr + text)
print("written", len(L), "blocks")
