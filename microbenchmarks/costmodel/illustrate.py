#!/usr/bin/env python3
"""Capture EXPLAIN (ANALYZE, BUFFERS, IO) text for the illustration queries:
plain index scan vs bitmap heap scan (+ Sort), fully cached and with the heap
uncached (index and VM warm), at effective_io_concurrency 100 and 16.
Outputs go to results/illustration/."""
import os
import sys
import json

sys.path.insert(0, "/mnt/nvme/postgresql/patch/claude_work/costmodel")
import rig  # noqa: E402

OUT = "/mnt/nvme/postgresql/patch/claude_work/costmodel/results/illustration"
os.makedirs(OUT, exist_ok=True)

QUERIES = [
    ("t_dups", "t_dups_grp",
     "SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp"),
    ("t_runs", "t_runs_id",
     "SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id"),
    ("t_sparse", "t_sparse_id",
     "SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id"),
]
PLANS = {
    "plain": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
    "bitmap": {"enable_indexscan": "off", "enable_seqscan": "off"},
}
STATES = [("cached", None), ("uncached_eic100", 100), ("uncached_eic16", 16)]


def explain_text(r, sql):
    with r.conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, IO) " + sql)
        return "\n".join(row[0] for row in cur.fetchall())


def main():
    r = rig.Rig()
    summary = {}
    for (table, index, sql) in QUERIES:
        for plan, gucs in PLANS.items():
            for state, eic in STATES:
                best = None
                for run in range(2 if state != "cached" else 3):
                    r.reset_gucs()
                    r.set_gucs(gucs)
                    r.set_gucs({"work_mem": "1GB", "max_parallel_workers_per_gather": 0,
                                "random_page_cost": 4.0, "seq_page_cost": 1.0,
                                "effective_io_concurrency": eic or 100})
                    if state == "cached":
                        r.prewarm(table)
                        r.prewarm(table, "vm")
                        r.prewarm(index)
                    else:
                        r.evict(table)
                        r.prewarm(table, "vm")
                        r.prewarm(index)
                    text = explain_text(r, sql)
                    ms = float([l for l in text.splitlines() if l.startswith("Execution Time")][0].split()[2])
                    if best is None or ms < best[0]:
                        best = (ms, text)
                name = f"{table}_{plan}_{state}"
                with open(os.path.join(OUT, name + ".txt"), "w") as f:
                    f.write(f"-- {sql}\n-- {plan} scan, {state}\n")
                    f.write(best[1] + "\n")
                summary[name] = best[0]
                print(f"{name:40} {best[0]:9.1f} ms", flush=True)
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
